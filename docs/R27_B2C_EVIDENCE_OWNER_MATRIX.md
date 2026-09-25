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
的实际进展记录在各家自己的 "B2C-n 进展" 小节（当前已到 **B2C-4B COMPLETE**）。

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
B2C-4A execution attribution fact completeness（**已完成**，见下文 "B2C-4A 进展"）
B2C-4B portfolio/accounting owner facts（**已完成**，见下文 "B2C-4B 进展"）
B2C-4C 迁移 pnl_attribution runtime（**已完成** —— 见下文 "B2C-4C 进展"；历史行情一条 OPEN PREREQUISITE）
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
B2C-4A execution attribution fact completeness（**已完成**）
B2C-4B portfolio/accounting owner facts（**已完成**）
B2C-4C 迁移 pnl_attribution runtime（**已完成** —— 见下文 "B2C-4C 进展"；历史行情一条 OPEN PREREQUISITE）
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
fill_verdict / code / action / requested_qty / filled_qty / fill_price / fees /
business_day / observed_at / inconsistencies），用
`json.dumps(sort_keys=True)` + sha256；刻意不用 `hash()`（跨进程不稳定）/ `repr(object)` /
地址 / 时间 / 随机数。verification statement 不重复进指纹 —— 它已经由
`OwnerVerification.canonical()` 单独进入 `fact_state`。

其中六个成交事实字段是 **B2C-4A** 追加的（见下文 "B2C-4A 进展"）：同一条 execution
identity 下数量 / 价格 / 费用 / 标的 / 方向被改写必须报 `EvidenceConflict`。它们以
`EvidenceField.as_dict()` 入指纹而不是 `maybe()`，因此 `unknown` 与 `not_applicable`
不会在指纹上撞成同一个值。

#### 本轮没有 production consumer（刻意）

B2C-3 的交付物是**能力 + 契约 + 回归**：factory 存在、research contract 支持 execution、
测试覆盖它。production runtime 仍然**没有**调用本 factory —— `pnl_attribution` 的迁移是
B2C-4。因此"本模块今天零调用点"是预期状态，不是空转。

**owner-origin provenance 仍然 OPEN / REQUIRED**（见上）。adapter 关闭的是"调用方不能自述
身份 / 业务日 / 核验结论"，**不是**"输入对象确实由 owner 产生"。B2C-3 不宣称关闭
two-step forgery。

### B2C-4A 进展（execution attribution facts 已发布）

#### 为什么把原 B2C-4 拆成三段

源码复核在动手前发现：**不能**把 `pnl_attribution` 直接迁到 execution 上。

```text
deepseek_research._pnl_evidence() 同时读四张表
    paper_accounts / paper_nav / paper_orders / paper_positions
其语义包括：NAV 变化、daily return、成交、fees、realized PnL、position cost / exposure
```

今天**只有 execution** 具备 typed owner contract + research adapter。所以"把整个 legacy
`_pnl_evidence` dict 塞进一个 execution `InformationEvent.payload`"会让 **NAV / position
cost / realized PnL / account state 冒充 execution owner 事实** —— 那正是 R27 禁止的
provenance 伪造。反过来，"为了现在就迁移而删掉这些能力"是**删 roadmap 能力**，同样禁止。

因此最终目标不变，只把实现顺序拆开：

```text
B2C-4A  execution attribution fact completeness      **COMPLETE**
B2C-4B  portfolio/accounting owner facts             **COMPLETE**
B2C-4C  pnl_attribution runtime migration            **COMPLETE**（历史行情一条 OPEN PREREQUISITE）
```

这是**实现顺序调整，不是 roadmap 缩减**。

#### 本段交付：投影足以承载 attribution 需要的成交事实

B2C-1 的 `ExecutionFactProjection` 只有 identity / lifecycle / verdict / PIT / verification，
因此 B2C-3 能证明"发生了 `partial` / `verified` / `not_executed`"，却**拿不出**
`pnl_attribution` 需要的成交数量、成交价格、费用、方向与股票代码。

B2C-4A 只关闭这个缺口，方式是**扩展既有投影**：

```text
ExecutionFactProjection 新增（顺序即 EXECUTION_FACTUAL_FIELDS）
    code · action · requested_qty · filled_qty · fill_price · fees

ExecutionFactProjection 新增（顺序即 EXECUTION_OWNER_FACT_FIELDS）
    account_id · cycle_id                 （与既有 business_day / observed_at 同组）
```

#### 为什么 account_id / cycle_id 必须归 execution（不能推给 B2C-4B）

它们是**跨 owner 的 join identity**：

```text
Execution owner:   这笔 order / fill 属于哪个 account / cycle
Portfolio owner:   这个 account / cycle 在 D 日的 cash / positions / realized pnl / NAV
```

前者属于 execution：`execution_planner` 早已把"订单归属哪个 account、属于哪个 cycle"当成
写入与成交的不变量。若不发布，B2C-4C 只能二选一：

```text
order_id → 重新查 paper_orders → account_id / cycle_id
    或
依赖调用方"记得自己刚才按哪个 account 过滤"
```

两条都在 typed owner projection 之外重建一条事实来源。而旧 `_pnl_evidence()` 明确按账户
归因、B2C-4B 的 portfolio fact 也以 `cycle_id / account_id / asof_day` 为上下文 ——
缺这两项，跨 owner 的 PnL join 无法完全由 typed facts 证明。

因此 `execution_evidence.py` 本轮改了**两处**（不是零处）：

```text
evidence_from_order()        provenance 带出 account_id / cycle_id（原样，不校验、不补值）
load_execution_evidence()    只读探测 paper_orders 是否已有 cycle_id 列；
                             没有就不请求它 → 该事实的周期归属如实报 unknown（不崩、不回填）
```

字段集、`fill_verdict`、`inconsistencies`、核验结论**零改动** —— 这也是该模块的只读护栏
（"证据模块只认识 orders + fills"）仍然成立的原因。

十条形态约束：

1. **不新增第二套 execution fact。** 刻意没有
   `ExecutionAttributionEvidence` / `TradeResearchEvidence` / `PnLExecutionEvidence` /
   `ExecutionResearchFact`。`ExecutionEvidence → ExecutionFactProjection` 仍是**唯一**
   execution fact contract。
2. **成交事实逐字派生，不重算。** `fact_projection` 直接复制 `evidence.code` / `action` /
   `requested_qty` / `filled_qty` / `fill_price` / `fees`；不查 DB、不重算价格或费用、
   不从 `paper_orders` 的兼容列补值。测试用 `assertIs` 锁住"同一个对象"。
3. **归属身份同样由 owner 派生。** `account_id` / `cycle_id` 取自订单行 provenance，
   同样三态、同样不由 caller 自述。缺失 / 不可证明 → `unknown`，**绝不**取
   "当前 active cycle / 当前账户 / `0` / `None`"做 fallback。
4. **必须是真三态字段。** 八个字段都是 `execution_evidence.EvidenceField`，
   `__post_init__` 要求 `isinstance(...)` 且 **name 精确匹配**；裸数字 / 裸字符串 / `None` /
   dict / duck-typed 对象 / 名字错位的真 `EvidenceField` 一律 fail closed。
   `known(0)` **不**退化成 `0`，**不**变成 `unknown`，`unknown` 也不许变成 `known(0)`。
5. **`as_dict()` 写 `EvidenceField.as_dict()`，不写 `maybe()`。**
6. **内容指纹随之扩展**（`ai_research_execution_adapter._content_fingerprint`）。
   同一条 execution identity 下数量 / 价格 / 费用 / 标的被改写、或这条成交被搬到另一个
   account / cycle → `fact_state` 不同 → `EvidenceConflict`。
7. **`ResearchEvidenceRef.detail` 不膨胀。** detail 仍然只放 identity / 核验 / 内容指纹 /
   最小审计元数据；八个字段**不**复制进去。
8. **合计 8 个字段，没有更多。** `order_time` / `reject_reason` / `cancel_reason` /
   `available_qty` / `commission` / `slippage` 都没有加进投影。

```text
ExecutionFactProjection      owner factual truth
InformationEvent.payload     一次 research observation 投影（B2C-4C 直接从投影产生）
ResearchEvidenceRef          identity + verification + fingerprint
```

#### 硬边界：哪些事实**不**属于 execution

```text
realized_pnl      ✗ 依赖 position cost basis / sell quantity / portfolio accounting
NAV / daily_pnl / daily_return / position_cost / market_value / unrealized_pnl /
benchmark / account cash                    ✗ 属于 portfolio/accounting owner
```

legacy `pnl_attribution` 确实读 `paper_orders.realized_pnl`，但 `realized_pnl` **不是纯
execution fact**。本段没有为了方便把它塞进 execution contract，也没有扩大成"复制整份
`ExecutionEvidence`"：除新增的八个字段外，`order_time` / `reject_reason` / `cancel_reason` /
`available_qty` / `commission` / `slippage` 都**没有**加进投影。

#### 核验语义与 PIT 语义一个字都没动

```text
verified + ledger       → verified
partial + ledger        → verified
not_executed + ledger   → verified
unknown + ledger        → unverified
legacy / absent / inconsistent → source_unusable
```

上表逐字不变。B2C-4A 只增加**事实内容**，不重新讨论**核验语义**。`business_day` /
`observed_at` 的派生方式与格式校验同样不变。

identity 的派生方式也不变：`source_id = <identity_kind>|<identity>` 仍然只由 owner 的
`event_key` 派生，`account_id` / `cycle_id` **不进入 identity** —— 它们进入的是**事实内容**
与内容指纹。这正是 EXEC-REF-26 / 27 要的语义：同一条 identity（同一次成交）被搬到另一个
account / cycle 时，identity 相同而**事实不同**，因此是冲突而不是新事实。

#### 本段仍然没有 production consumer（刻意）

`evidence_ref_from_execution_projection` 在 production 里的调用点仍然 **0**：
`pnl_attribution` 的迁移是 **B2C-4C**，组合/记账事实是 **B2C-4B**。`deepseek_research`、
`adaptive_engine`、`ai_analysis` 本段**未改动**。

**owner-origin provenance 仍然 OPEN / REQUIRED。** 本轮增加字段**不等于**关闭
`ExecutionEvidence 公开构造 → fact_projection → research adapter` 这条两步伪造路径。
本段不把它改成 CLOSED。

#### B2C-4B 的剩余 owner gap（提前写清）

legacy `pnl_attribution` 当前仍依赖、且**不能**由 execution adapter 冒充的事实：

```text
latest / prior NAV
daily PnL
daily return
realized PnL
position cost summary
account / cycle context
```

优先复用**已经存在**的 `paper_portfolio_read_model.py`（R22 已建立 cycle/as-of bounded
portfolio read model）：

```text
PortfolioReadContext
positions_for_context_with_status
realized_pnl / cash / portfolio_for_context
STATUS_VERIFIED / STATUS_UNKNOWN
```

B2C-4B 应在这个既有 owner 上建立 typed portfolio/accounting fact projection，而不是新增
`new_pnl_repository` / `pnl_fact_manager` / `pnl_owner_service`。

**一条必须提前守住的限制**：`portfolio_for_context(... valuations=...)` 今天仍接受
**调用方提供**的 valuation Mapping。因此 B2C-4B **不得**把"调用了 `portfolio_for_context`"
当成"market valuation 的 owner provenance 已成立" —— market valuation 的来源仍必须来自
**R24 typed market fact**。B2C-4B 要继续区分：

```text
portfolio ledger authority        （持仓数量 / 成本 / 已实现盈亏 / 现金）
market valuation authority        （市值 / 未实现盈亏 / NAV 的估值腿）
```

不把两者揉成一个假 owner。

### B2C-4B 进展（portfolio/accounting owner facts 已发布）

#### 状态

```text
B2C-4A  execution attribution fact completeness       COMPLETE
B2C-4B  portfolio/accounting owner facts              COMPLETE   ← 本节
B2C-4C  pnl_attribution runtime migration             COMPLETE（历史行情一条 OPEN PREREQUISITE）
```

本段是**能力 + 契约 + 回归**，不是 runtime 迁移：`deepseek_research._pnl_evidence()` 与
`paper_nav` writer / `paper_positions` writer **一行未改**。真正的 consumer 迁移是 B2C-4C。

#### owner 仍然是既有模块（没有第二个 portfolio owner）

```text
backend/paper_portfolio_read_model.py
    PORTFOLIO_FACT_CONTRACT_VERSION      = "portfolio-fact-v1"
    PORTFOLIO_FACT_VERIFICATION_SCOPE    = "cycle_account_asof_accounting"
    PORTFOLIO_FACT_KINDS                 = cash / realized_pnl / position_cost_summary
    PORTFOLIO_FACT_STATUSES              = verified / unknown
    PositionCostSummary                  （position_count + cost_value，纯 value type）
    PortfolioFactProjection              （无 public raw 构造器；私有签发口）
    accounting_fact_projections(conn, context, *, account_id)   ← 唯一 public 入口
```

刻意**没有** `pnl_repository.py` / `pnl_service.py` / `portfolio_fact_manager.py` /
`accounting_facade.py` / `research_portfolio_repository.py` / `BaseOwnerAdapter` /
adapter registry framework。portfolio/accounting 的 factual authority 仍然**只有一个**：
`paper_portfolio_read_model`。

#### 依赖方向

```text
verified execution / durable lots
          ↓
paper_portfolio_read_model            （owner：事实、身份、业务日、核验结论）
          ↓
PortfolioFactProjection               （owner fact contract）
          ↓
ai_research_portfolio_adapter         （唯一接缝：owner → research 翻译）
          ↓
ai_research_contract.ResearchEvidenceRef
          ↓
InformationEvent.payload              （B2C-4C 再从投影产生）
```

owner **不得** import research，research 契约**不得** import DB-backed owner。
`ai_research_portfolio_adapter` 是唯一同时认识两套词表的 production 模块。

#### 本段发布的事实（全部 bounded by cycle + account + asof）

```text
cash                      bounded 重建现金（cycle/account 初始本金 + 已验证成交现金流）
                          ≠ paper_accounts.cash（当前可变状态，不是历史 authority）
realized_pnl              已验证 SELL 的 owner realized_pnl 合计
                          （cycle bounded / as-of bounded / incomplete sell fail closed）
position_cost_summary     durable lots 的开仓数与成本合计
                          = 开仓（remaining_qty>0）的唯一 account/code 数 + Σ qty×cost
```

三条事实**顺序固定**（cash → realized_pnl → position_cost_summary），便于 deterministic
fingerprint 与调用方读取。

**必须先证明归属**：`accounting_fact_projections()` 先要求 `_cycle_initial(...)`
（内部即 `paper_accounts` 的 cycle 绑定 + `_account_attached_by`）能证明该 account 属于该
cycle 且在 `asof_day` 前已挂载。证明不了时三条事实**全部** `unknown` 且 `value=None` ——
绝不退化成"这个账户没有卖出，所以已实现盈亏 = verified 0"。这是本段最容易做错的一处：
单独调 `realized_pnl()` 时，"账户存在但没卖出"与"账户压根不存在"都可能返回
`0.0, verified`。

**结构性 fail closed**：`status == unknown` 时 `value` **必须**是 `None`（构造期强制）；
数值事实必须是有限浮点数；`position_cost_summary` 的值必须是 typed `PositionCostSummary`。
`status=unknown, value=123.45` 在类型层面不可表达，消费者无法忽略 status 偷用一个没有被
证明的数字。反之 `verified 0.0` **不是** `unknown` —— 肯定性的零必须保留。

#### 为什么 NAV **不是** portfolio-only 事实（本段最重要的边界）

`portfolio_for_context(...)` 今天允许调用方显式传入 `valuations: Mapping`。那只能证明
"调用方给了一个合法数字"，**不能**证明：

```text
这些价格来自 R24 owner
这些价格通过了哪套 verification
这些价格对应哪个真实 market snapshot
```

因此 `portfolio_for_context(..., valuations={"600000": 12.3})` 返回的
`market_value_status = verified` / `nav_status = verified` **不得**被包装成 owner-verified
research fact —— 那会把"caller 给了一个合法数字"错误升级成"R24 market owner 已证明这条
估值"。于是本段的 typed projection **禁止包含**：

```text
nav · latest_nav · prior_nav · daily_pnl · daily_return
market_value · unrealized_pnl · benchmark · valuation price · quote_status
```

也**禁止接受** `valuations` Mapping / `MarketDataReading` / current quote / latest quote 作为
portfolio fact factory 的参数（签名里只有 `conn` / `context` / `account_id`）。

正确的后续组合是跨 owner 的：

```text
execution owner facts + portfolio/accounting owner facts + R24 market owner facts
        ↓
B2C-4C cross-owner pnl_attribution composition
```

B2C-4C 若拿不到能证明**对应业务日**的 R24 valuation，结论必须是 `unknown`，而不是
current quote fallback / cost fallback / `paper_nav.quote_status == "verified"` 冒充
canonical market provenance。

#### `paper_nav` 本轮继续是 legacy / compatibility，不升级成 owner fact

当前 `paper_nav` writer 的估值允许 `local_snapshot_fallback` / `cost_fallback`，且
`quote_status` 取值 `verified` / `cost_fallback`。这个 `quote_status="verified"`：

```text
不是 R24 OwnerVerification
不是 cross-source verification
没有 typed market evidence identity
```

所以**禁止**写 `paper_nav.quote_status == "verified"` → research owner fact verified。
本段不读 `paper_nav` 签发任何 typed evidence，也不删除 / 迁移 `paper_nav` writer、不改
`_record_nav` —— 实际的 runtime convergence 留给 B2C-4C。

#### `paper_positions` 继续是 compatibility-only 投影

R22 已明确 `paper_positions = compatibility-only projection`。typed portfolio facts 建立在
`paper_position_lots` + 已验证 fills/orders + cycle/as-of bounded reconstruction 之上；
`position_cost_summary` 从 `bounded_lots_with_status(...)` 派生，**不读**
`SELECT ... FROM paper_positions`（它甚至没有 `cycle_id` 列，也不携带 as-of 证据）。

#### 唯一 research adapter

```text
backend/ai_research_portfolio_adapter.py
    __all__ = ("evidence_ref_from_portfolio_projection",)
```

依赖方向 `paper_portfolio_read_model → ai_research_portfolio_adapter → ai_research_contract`。
复用既有的 `EVIDENCE_SOURCE_PORTFOLIO_RESEARCH`（`"portfolio_research"`）与
`EVENT_PORTFOLIO_RESEARCH_OBSERVED`，**不**新增 `portfolio_accounting` / `portfolio_fact` /
`pnl_fact` / `accounting_research` 这类第二套 source type。

`SUPPORTED_OWNER_ADAPTERS` 因此变成：

```text
market_data
execution
portfolio_research
```

adapter 刻意**不**新增 `PortfolioResearchService` / `PortfolioAdapterManager` / `BaseAdapter` /
registry framework / repository / facade。

#### status → owner-neutral outcome：显式穷尽表

```text
STATUS_VERIFIED → OWNER_OUTCOME_VERIFIED
STATUS_UNKNOWN  → OWNER_OUTCOME_UNVERIFIED
```

刻意**不**产生 `source_unusable`：本 owner 今天没有发布"证据源不可用"这个独立状态，
research 侧不得替它猜一个。映射是一张**完整表**（`_PORTFOLIO_OUTCOME_BY_STATUS`），并且
每次签发前做**双向**一致性检查（`mapping keys == PORTFOLIO_FACT_STATUSES`，不缓存）：
owner 新增一个状态时 adapter 在**下一次调用**就 fail closed，而不是静默落进 `else`
被当成 `unverified`。**禁止** `else: outcome = OWNER_OUTCOME_UNVERIFIED`。

#### identity / 内容指纹 / detail

```text
source_type = portfolio_research
source_id   = <fact_kind>|cycle=<cycle_id>|account=<account_id>
as_of       = projection.asof_day（= PortfolioReadContext.asof_day）
```

调用方**不能**传 `source_id` / `as_of` / `status` / `outcome` / `cycle` / `account`。
`as_of` 只能来自 owner context —— 没有 `created_at` / `updated_at` / `today()` /
latest NAV date fallback。

内容指纹覆盖 fact contract 版本 / `fact_kind` / `cycle_id` / `account_id` / `asof_day` /
**canonical value**，用 `json.dumps(sort_keys=True, separators=(",", ":"))` + sha256；
刻意不用 `hash()` / `repr(object)` / 内存地址 / 当前时间 / 随机数。verification statement
不重复进指纹：它已经由 `OwnerVerification.canonical()` 参与 `fact_state()`。

`ResearchEvidenceRef.detail` 只放 `content_fingerprint` / `contract_version` /
`read_model_version`，**不**复制 owner 的 factual payload（cash / realized_pnl /
position_count / cost_value）。分层仍然是：

```text
PortfolioFactProjection   owner factual truth
ResearchEvidenceRef       identity + verification + fingerprint
InformationEvent.payload  一次 research observation 投影（B2C-4C 再建立）
```

`OwnerVerification.attributes` 只有 `verification_scope` / `fact_contract_version` /
`read_model_version` / `fact_kind`；刻意没有 `confidence` / `score` / `quality_score`，
也没有 market-only 的 `verification_method` / `cross_source_verified`（对非 market owner
它们保持"不适用"：`None` / `False`，不是"核验失败"）。

#### provenance 的诚实声明（不许含糊）

```text
contract-issued portfolio projection:                    CLOSED
caller self-declared identity / status / as_of:          CLOSED
physical database origin / trusted database provenance:  OPEN / REQUIRED
```

调用方仍然可以自造 SQLite connection / fixture 并调用 owner 的 public read 拿到投影。本段
**不**声称"all owner-origin provenance solved"，R27 的总目标也不因此降低。

#### 本段仍然没有 production consumer（刻意）

```text
evidence_ref_from_portfolio_projection   production callers = 0
EXPECTED_EVENT_CONSTRUCTORS              不变（deepseek_advisor 仍是唯一）
deepseek_research / paper_trading        未改动
```

B2C-4B = capability + contract；runtime consumer migration = B2C-4C。若本轮就出现
`deepseek_research` / `ai_analysis` / `adaptive_engine` 的生产调用，那是 scope violation。

#### 回归门禁（B2C-4B）

`backend/test_portfolio_fact_contract.py`：PFACT-01（public 构造器被拒绝）、PFACT-02
（kind/status 闭集，非法值 / 非有限值 / 类型错位的值全部在构造期 fail closed）、PFACT-03
（cycle/account/asof 由 context 派生，签名零 fallback）、PFACT-04（现金来自 bounded 重建，
`paper_accounts.cash` 不能覆盖它）、PFACT-05（只认已验证 SELL；未验证 SELL 即整体
fail closed）、PFACT-06（不存在 / 未挂载账户绝不变成 verified zero）、PFACT-07（持仓成本
来自 durable lots，不是 `paper_positions`）、PFACT-08（数量未证明 → unknown/None）、
PFACT-09（未来 fill/lot 不进当日事实）、PFACT-10（archived / 不可证明 context 全部
fail closed）、PFACT-11（非有限账本值不得成为 verified fact）、PFACT-12（已验证的零不得被
误判成 unknown）、PFACT-13（固定且确定性的发布顺序）、PFACT-14（投影没有 NAV /
market_value / unrealized / daily_return / quote_status 表面）、PFACT-15（factory 不接受
valuations / current quote / latest fallback —— 断言只看**会执行**的代码，不看 docstring）。

`backend/test_ai_research_portfolio_adapter.py`：PORT-REF-01 ~ 04（只接受真投影、
`source_type = portfolio_research`、identity 完全由 kind/cycle/account 派生、`as_of` 完全
来自 owner context）、PORT-REF-05 ~ 06（verified → verified；unknown → unverified 且**不是**
source_unusable）、PORT-REF-07 ~ 08（映射与 owner 公开闭集双向穷尽；模拟 owner 新增状态
必须 fail closed）、PORT-REF-09 ~ 12（同 identity 下 cash / realized PnL / position summary
被改写 → `EvidenceConflict`；不同 account/cycle/kind → 不同 identity）、PORT-REF-13（detail
不复制 factual payload）、PORT-REF-14（不引入 market 核验词汇）、PORT-REF-15
（`InformationEvent.kind = portfolio_research_observed`）、PORT-REF-16（production 调用点
= 0）、PORT-REF-17（签名不给自述入口）、PORT-REF-18（依赖方向单向 + provenance 仍 OPEN）。

`backend/test_ai_research_evidence_ownership_guard.py`：`EXPECTED_OWNER_FACTORIES` 增加
`portfolio_research`，`APPROVED_ISSUER_CALLERS` 增加
`ai_research_portfolio_adapter.py → evidence_ref_from_portfolio_projection`；最终精确的
caller set 只有三个（market / execution / portfolio），**不能多、不能少**。负向用例覆盖：
本地同名 portfolio factory 被拒、其它模块同名被拒、portfolio adapter 偷调 market/execution
的 factory 被拒、contract 模块偷导出 portfolio factory 被拒。只做 module + symbol /
module + enclosing function 结构扫描，不扩展成 CFG / dataflow scanner。

语义 mutation 在 `work/r27b2c4b_portfolio_accounting_mutation_check.py`：移除归属证明、
现金改读 `paper_accounts.cash`、持仓成本改读 `paper_positions`、数量未知时仍发 summary、
去掉 as-of 边界、`realized_pnl` 绕过 verified 口径、adapter 把 unknown 映成 verified、
`source_id` 丢掉 account、指纹忽略 factual value、adapter 接受 duck-typed —— 必须全部
CAUGHT（survived = 0、fake = 0、restore sha256 一致）。矩阵用 `--non-vacuity` 跑：每条先跑
baseline，因此**目标用例路径写错会被报成 BASELINE-RED 而不是静默通过**。

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

> **R27-B2C-5 复查更正（不删上方原文，保留当时判断依据）**：上表是"research 侧还没有
> owner-native 归口"时的判断，逐条事实仍然成立。R27-B2C-5 重新审计后把**可迁移的那部分**
> 交付了：news owner 发布 typed fact contract + 唯一 adapter，并且**只**用
> `owner-neutral unverified` / `source_unusable` 归口 —— 既不发明核验结论，也不把
> `evidence_grade` 当核验。见文末 [B2C-5 进展](#b2c-5-进展news-owner-readiness)。

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
**零调用点**（迁移是 B2C-4C）。B2C-4A 进一步让该投影携带 attribution 需要的成交事实；
B2C-4B 为同一目标补上**第二个 owner**（`portfolio_research`）—— portfolio/accounting 的
typed 记账事实与它自己的 adapter，同样**零调用点**。三类 typed fact（execution /
portfolio accounting / R24 market）齐备之后，B2C-4C 才能做跨 owner 组合。

### 建议的迁移顺序（与 §六 的排除项一致）

```text
1. pnl_attribution        拆成三段：
                           B2C-4A execution capability（已完成）
                           B2C-4B portfolio/accounting owner facts（已完成）
                                 只发布 ledger owner 能证明的记账事实；
                                 NAV / 市值 / 未实现盈亏 / 日 PnL 留给跨 owner 组合
                           B2C-4C runtime 迁移（前提：A + B 都完成）
                                  需要执行 + 组合记账 + R24 估值三类 typed fact 同时到位
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

### B2C-4A 之后的数字（能力补完，架构面中性）

```text
Roadmap capability removed:                        0
Roadmap invariant weakened:                        0
Production modules added:                          0
Production modules removed:                        0
New service / manager / repository / facade:       0
New owner:                                         0
Execution fact authorities:                        before = 1   after = 1
Execution research adapters:                       before = 1   after = 1
Execution factual fields published:                before = identity / lifecycle / verdict /
                                                             PIT / verification
                                                   after  = 上面 + code / action /
                                                             requested_qty / filled_qty /
                                                             fill_price / fees /
                                                             account_id / cycle_id
Execution verification semantics changed:          NO
PIT semantics changed:                             NO
Execution identity derivation changed:             NO（account/cycle 进的是事实内容，不是 identity）
Existing modules touched:                          execution_evidence.py（2 处：provenance
                                                   带出归属身份；读路径探测 cycle_id 列）
Runtime migrations:                                0
Legacy pnl writer changed:                         NO
deepseek_research 新增 order/fill SQL:             0
Net architecture surface:                          NEUTRAL
```

这一轮**不是**新增一层，而是让既有 owner contract 达到原 roadmap runtime 需要的完整度：
投影仍然只有一个发布入口、一个 authority、一个 adapter。`realized_pnl` / `NAV` /
position cost 没有被塞进来，它们留给 B2C-4B 的 portfolio/accounting owner。

### B2C-4B 之后的数字（能力补完 + 一个真实 adapter 接缝）

```text
Typed fact owners:                                  before = 2   after = 3   （+ portfolio_research）
Public evidence factories:                          before = 2   after = 3   （+ portfolio_research）
Owner verification model:                           owner-neutral（B2C-2 起不变）
Portfolio/accounting fact authority:                before = paper_portfolio_read_model
                                                    after  = paper_portfolio_read_model（未迁移）
Research-owned portfolio status semantics:          0（状态词只出现在 adapter 的归口表）
Portfolio → market vocabulary translation:          0
Production modules added:                           1（ai_research_portfolio_adapter）
Production modules removed:                         0
New service / manager / repository / facade:        0
Registry framework / BaseAdapter:                   0
Second portfolio owner / repository:                0
Portfolio fact kinds published:                     cash / realized_pnl / position_cost_summary
NAV / market_value / unrealized_pnl published:      0
paper_nav used to issue typed evidence:             NO
paper_positions used as typed fact authority:       NO
Raw valuations accepted by the fact factory:        NO
deepseek_research / paper_trading changed:          NO
paper_nav writer changed:                           NO
paper_positions writer changed:                     NO
DB migration / API change / frontend change:        0
Runtime migrations:                                 0
Production adapter callers:                         ≥1（R27-B2C-4C 已接线，见 "B2C-4C 进展"）
Modules needed to understand portfolio → evidence:  3
                                                        paper_portfolio_read_model
                                                        ai_research_portfolio_adapter
                                                        ai_research_contract
Net architecture surface:                           INCREASED（有理由）
```

架构面**增大**了，而且是**有理由的**：新增的是 roadmap 明确要求的 owner→research 接缝，不是
forwarding layer —— `ai_research_contract` 不得 import DB-backed 的
`paper_portfolio_read_model`，而 portfolio owner 也不得 import research。三类事实（execution /
portfolio accounting / R24 market）在 B2C-4C 汇合成 `pnl_attribution` 之前，这条接缝必须
存在，而且**只能有一条**。

它**不**声称第 2 层（owner-origin provenance）已经成立：

```text
contract-issued portfolio projection:                    CLOSED
caller self-declared identity / status / as_of:          CLOSED
physical database origin / trusted database provenance:  OPEN / REQUIRED
```

**Roadmap capability removed = 0；Roadmap invariant weakened = 0。** 本轮没有把
`NAV = portfolio-only authority` 写进任何地方，也没有删掉 `pnl_attribution` 仍需要的
NAV / daily PnL / daily return 能力 —— 它们只是被正确地留在跨 owner 组合那一步（B2C-4C）。

### B2C-4C 进展（pnl_attribution 的 runtime 已迁到 canonical typed 事实）

`deepseek_research._pnl_evidence()` 原来同时是 DB reader、事实 owner、PIT 推断、
跨 owner 组合与 research 投影。四条 legacy SQL（`paper_accounts` / `paper_nav` /
`paper_orders` / `paper_positions`）**已全部删除**，现在这条路径只消费 owner typed 事实：

```text
execution owner      execution_evidence.load_execution_evidence
                     → execution_verification.fact_projection
                     → ai_research_execution_adapter.evidence_ref_from_execution_projection
portfolio owner      paper_portfolio_read_model.accounting_fact_projections
                     → ai_research_portfolio_adapter.evidence_ref_from_portfolio_projection
R24 market owner     market_data_service.read_snapshot(ATTRIBUTION_POLICY, now=…, asof_day=…)
                     → ai_research_contract.evidence_ref_from_market_reading
                     ↓
             ARC.InformationEvent（每一条都携带 evidence_ref）
                     ↓
             pnl_attribution compatibility projection（展示层，非 authority）
```

**固定矩阵（谁拥有什么）**：

| 事实 | owner | typed 来源 |
|---|---|---|
| code / action / requested_qty / filled_qty / fill_price / fees | execution | `ExecutionFactProjection`（`EvidenceField` 三态） |
| account_id / cycle_id / business_day / observed_at | execution | 同上（owner 记录的订单行派生，缺失即 `unknown`） |
| cash / realized_pnl / position_cost_summary | portfolio/accounting | `PortfolioFactProjection`（cycle + account + asof 定界） |
| valuation observation / market verification | R24 market | `MarketDataReading`（`verification` + `verification_method` 一起发布） |
| latest NAV（= 组合账本 + R24 估值） | **组合** | `portfolio_for_context(..., valuations=…)`，valuations 只能由组合层内部从 reading rows 构造 |
| prior NAV / daily PnL / daily return | **组合** | 需要前一业务日 + 前一组合状态 + 前一 R24 reading —— 见下 |
| 填充与去重/冲突判定 | research composition | `InformationEvent` + `EvidenceConflict`（不是新的 owner） |

**PIT context 是显式的**：`deepseek_research.AttributionRequest(asof_day, market_now,
targets)` 三个字段都没有默认值 —— 业务日、`(account_id, cycle_id)` 与市场观测时刻全部由
编排边界（`adaptive_engine`）声明，collector 不再用 `max(paper_nav.nav_date)` / `today()` /
current cycle / current active account 推断任何一项；缺 context 即 fail closed（记
`_collection_error`），不发布"看起来正常"的归因。

生产里唯一的签发口是 `adaptive_engine._post_close_attribution_request(now=…)`：

```text
now      必填的显式观测 instant（tz-aware）—— 签发口自己不读墙钟；无参调用直接 TypeError
asof_day 必须同时满足两条才允许签发：
         ① 交易日历判定它是完成交易日（universe.latest_complete_trade_date(now=now)：
            周末/法定假日不是，15:05 前的当日也不是）
         ② 它就是调用方声明的那个**本日历日**
         任一不满足 → fail closed（返回 None），**不**退到"最近已完成交易日"
         ⇒ asof_day 恒等于本日历日；签名里也没有业务日参数
targets  来自 paper_accounts.cycle_id（**当前**绑定）
         ⇒ 只支撑"当日 post-close"；签发后是不可变快照，之后解绑/换周期不会重绑定
```

为什么必须拒绝那个回退：交易日历在盘中/周末/节假日会返回**上一个已完成交易日**。照此签发
就会得到"上一交易日 asof + 当前 cycle 绑定"——账户后来解绑或换周期后，那就是用当前归属解释
历史日，即 current-state leak。因此这条限制是**运行期强制**的（不满足即 fail closed），而不是
只在签名上"不提供"。另外生产里只有 `adaptive_engine` 能构造 `AttributionRequest`。

历史归因需要 owner **可证明的历史挂载证据**：

```text
OPEN PREREQUISITE: owner-provable historical cycle membership for back-dated attribution
```

届时应由调用方**显式给出 targets**，而不是让边界去自动发现。

**OPEN PREREQUISITE（不是 REMOVED）**：

```text
OPEN PREREQUISITE: historical market owner evidence required for prior-day valuation
```

R24 今天只有**当前** full-market snapshot（`read_snapshot` 支持 `asof_day` fail-closed 检查，
但这不等于拥有任意历史日 archive）。因此 `prior_nav` / `daily_pnl` / `daily_return` 在拿不到
可证明的前一业务日 R24 valuation 时如实报 `unavailable` +
`historical_market_evidence_unavailable`，**字段位置保留**：不删 schema、不回落
`paper_nav LIMIT 2`、不拿 D 日行情回填 D-1。这条能力由后续 PIT/market archive 阶段关闭，
本轮不顺手实现历史行情系统。

**本轮的 shape 变化（显式记录，不静默）**：

```text
accounts[].name / status / version     →  已移除（它们是 current metadata，没有
                                          cycle + asof 历史契约，不得进入 canonical
                                          historical evidence；旧 shape 依赖见 compatibility
                                          projection 的 presentation_authority / is_authoritative）
accounts[].nav_observations            →  已移除（legacy 的 paper_nav 行数不是事实）
asof                                   →  改由显式 context 提供（原来是 max(nav_date)）
filled_trades[].amount                 →  仅当 filled_qty 与 fill_price 都 owner-known 时派生，
                                          并标 amount_basis=derived_filled_qty_times_fill_price
fees / realized_pnl                    →  unknown 时不再补零，而是 None + availability 原因码
```

**Legacy surface removed = YES**（pnl 路径的 direct-SQL authority）；**Roadmap capability
removed = 0**；**Original invariant weakened = NO**。

---

## B2C-5 进展（news owner readiness）

owner 是 `news_learning` 的 durable event ledger。本轮**只**建立 owner→research 接缝，
**不**迁移 `deepseek_research._event_evidence` 的 runtime（那是后续 event_evidence
convergence），因此 news adapter 的 production 调用点今天仍然是 **0**。

### NEWS OWNER MATRIX

| table / fact | writer | identity | PIT availability | verification input | canonical owner? | target treatment |
| --- | --- | --- | --- | --- | --- | --- |
| `news_events` 公司级公告/新闻事件 | `news_learning.capture_events:465`（`INSERT OR IGNORE`） | `event_key` UNIQUE（`sha256(source_name\|code\|identity)`，`identity = source_url \|\| article_id \|\| normalized_title`） | **`first_seen_at`**（`seen = first_seen_at or _now()`，`INSERT OR IGNORE` ⇒ 不可被后一次抓取覆盖） | 无核验列；`source_url` / `article_id` 决定**可追溯性** | ✅ `news_learning` | **TYPED EVENT FACT** |
| `market_major_events` 市场级重大事件 | `news_learning.capture_major_events:542`（`INSERT OR IGNORE`） | `event_key` UNIQUE（`sha256(source_name\|identity)`） | **`first_seen_at`** | `verification_status` NOT NULL，唯一 writer 的内联表达式只写出 `single_source_linked` / `unverified`；`evidence_grade` | ✅ `news_learning` | **TYPED EVENT FACT** |
| `market_event_candidate_links` 事件↔候选/行业映射 | `capture_major_events:587`（`INSERT OR IGNORE`） | `(event_id, code)` PK | `created_at`（不是事件可用性） | 启发式 `confidence`（0.95 直接标签 / 0.72 行业映射） | ✅ `news_learning` | **PRESENTATION / CONTEXT**（**不是** event truth，**不是** causal verification） |
| `news_source_reputation` 来源级聚合统计 | `news_learning.recalibrate:695`（**DELETE + INSERT**，可变快照） | `source_name` PK | 无（`updated_at` 是 now，没有 as-of） | `credibility_score` 是确定性公式，输入含调用方 grade | ✅ `news_learning` | **OWNER METADATA**（**不是** per-event verification） |
| `news_event_outcomes` 事件后 1/3/5 交易日结果 | `mature_outcomes:639` | `(event_id, horizon)` PK | 严格取 `first_seen_at` 之后的交易日 | 无核验维度；`price_source` 是自由字符串 | ✅ `news_learning` | **DERIVED LEARNING METADATA** |
| `news_effectiveness` 事件类型 × grade 效果统计 | `recalibrate:723`（DELETE + INSERT） | `(event_type, evidence_grade, horizon)` PK | `updated_at`（无 as-of） | 无 | ✅ `news_learning` | **DERIVED LEARNING METADATA** |
| `news_factor_versions` news-learning overlay 版本 | `recalibrate:763` | `version` UNIQUE | 只有 `created_at` | `status ∈ {shadow, micro_eligible}` 是**门禁词**，不是核验词 | ✅ `news_learning` | **OWNER METADATA / OUT OF EVENT EVIDENCE SCOPE** |
| `news_learning_runs` 运行台账 | `run_cycle:852/:858` | `id` | `started_at` / `finished_at` | 无 | ✅ `news_learning` | **OUT OF B2C-5 SCOPE** |
| `news_candidate_snapshots` 候选池快照 | `capture_candidate_snapshot:321`（唯一真 upsert） | `(snapshot_date, slot, account_id, code)` UNIQUE | `captured_at` / `valid_until` | 无 | ✅ `news_learning` | **OUT OF B2C-5 SCOPE**（不是事件事实） |
| 公司公告 fetch 端的 `"verified": True` | `data_fetcher.fetch_company_announcements:1653` | — | — | 抓取端布尔，**落库时被丢弃**（`news_events` 没有该列） | ❌ 不是 owner 事实 | **NOT VERIFICATION**（不得持久化、不得引用） |

### 与上表对应的强制边界

```text
availability authority                     first_seen_at（exact timestamp）
availability_day 口径                      first_seen_at 归一到 **owner 时区**（Asia/Shanghai）
                                           之后的日历日；与 read 的 as_of+23:59:59+08:00
                                           日边界是同一套口径
published_at                               描述性来源时间（只进 payload / detail，永不进 as_of）
evidence_grade                             provenance / traceability 元数据，**不是**核验
market_major verification_status           owner 词汇；当前**审计过的** writer 闭集
                                           {single_source_linked, unverified}
single_source_linked                       → OWNER_OUTCOME_UNVERIFIED（**不是** verified）
candidate mapping confidence               → 不是 event verification
source reputation credibility_score        → 不是 event verification
typed read 读的表                          只读 news_events / market_major_events
typed read 的网络访问                      0（不联网、不建表、不回填）
adapter                                    ai_research_news_adapter（唯一接缝）
runtime `_event_evidence` 迁移              OPEN / later（本轮**没有**迁移）
```

**复审修正（#206 review）—— 跨 offset 的 `availability_day` 是 PIT blocker**：第一版的
`_parse_owner_instant` 返回**未归一**的 instant，于是 `availability_day` 取自**原始
offset** 的 `.date()`，而 read 的日边界取自上海日末 —— 同一件事有了两套日历口径。一个
`first_seen_at = 2026-09-20T16:30+00:00`（上海其实是 9/21 00:30）的事件会读出
`availability_day = 2026-09-20`，让 `ResearchEvidenceRef.as_of` 声明一个**比真实可用日更早**
的业务日，进而使 `InformationEvent(as_of="2026-09-20")` 通过 look-ahead guard。修正：owner
instant 解析后 `astimezone(TZ)` 再派生日历日；并用 `NEWS-34`（含反向对照
`2026-09-21T01:00+14:00` ⇒ 上海 9/20 19:00 ⇒ 业务日 9/20）与 `M-NEWS-18` 双向钉住。

`CURRENT NEWS OWNER HAS NO VERIFIED STATE` 是本轮审计出来的**事实**：整个仓库只有一处
`verification_status` writer，没有任何 UPDATE / 第二 writer / 多源复算路径能把它升级，
`news_events` 连核验列都没有。因此 owner 的核验闭集刻意只有三态、且没有 verified ——
发明一个就是伪造 provenance。

### B2C-5 之后的数字

```text
news factual owners:                                before = 1（news_learning）  after = 1（未迁移）
news typed fact contract:                           before = 0                  after = 1
news adapter count:                                 before = 0                  after = 1
news adapter production callers:                    before = 0                  after = 0（预期状态）
duplicate news ledgers:                             before = 0                  after = 0
production modules added / removed:                 1（ai_research_news_adapter）/ 0
new typed projection modules added:                 0（住在既有 news_learning 里）
new service / manager / repository / facade:        0
DB migration / schema change:                      0（沿用既有 event_key / first_seen_at / evidence_grade / verification_status）
network-capable typed read paths:                  0
implicit historical time fallbacks:                0
deleted roadmap capability:                        0
weakened original invariant:                       NO
```

**Roadmap capability removed = 0；Original invariant weakened = NO；Net architecture surface =
由 NEUTRAL 变为 INCREASED（有理由）** —— 增大的那一份是 roadmap 明确要求的
owner→research 接缝（`ai_research_contract` 不得 import DB-backed 且会联网的
`news_learning`，news owner 也不得 import research），不是新增第二个 news owner。

### 已知 OPEN 项（不是 roadmap 删除）

```text
OPEN / REQUIRED:
deepseek_research._event_evidence still contains legacy direct-ledger reads
and live-fetch fallback（没有 durable events 时会去抓 live news）。

该 fallback **不**代表 canonical news evidence path：canonical 路径是
news_learning.news_fact_projections（纯读 durable ledger）
  → ai_research_news_adapter.evidence_ref_from_news_projection。

event_evidence runtime convergence = DEFERRED（不是 COMPLETE）
```

B2C-5 只能写 **news owner readiness = COMPLETE**，不能写"news runtime 已完全迁移"。

