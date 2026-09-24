# feat(ai): adapt execution facts into typed research evidence

R27-B2C-3。把 **execution owner 的 typed fact 投影**接进 research evidence，并且**只**做这一件事。

```text
R27-B2C-1  COMPLETE   execution owner fact contract
R27-B2C-2  COMPLETE   owner-neutral research verification
R27-B2C-3  COMPLETE   execution fact adapter        ← 本 PR
R27-B2C-4  NOT STARTED pnl_attribution runtime migration
```

```text
MERGE: NOT MERGED
DEPLOY: NOT DEPLOYED
```

---

## Scope

**做**：新增**一个且只有一个** production 模块
`backend/ai_research_execution_adapter.py`，公开**一个**函数
`evidence_ref_from_execution_projection(projection)`；在 research 契约的
`SUPPORTED_OWNER_ADAPTERS` 里登记 `execution`；把私有签发口边界从"契约外零调用"升级成
`(模块, enclosing function)` **精确 allowlist**；把 `owner → factory` registry 升级成
`owner → (module, factory)`。

**不做**（逐条确认）：

```text
pnl_attribution runtime migration          → B2C-4
deepseek_research runtime changes          → B2C-4 之后
InformationEvent production path migration → 不涉及（构造点集合不变）
provider prompt changes                    → 0
frontend                                   → 0
B3 / R28                                   → 未开始
新增 service / manager / repository / facade / registry framework / BaseAdapter → 0
修改 execution_verification.py / execution_evidence.py → 0（owner contract 已足够）
```

本 PR 在生产里**没有**调用 execution factory。这是刻意的：B2C-3 的交付物是
**能力 + 契约 + 回归**，不是接线。`pnl_attribution` 的迁移是 B2C-4。

---

## Dependency direction

```text
execution_verification                  owner：事实 / 身份 / 业务日 / 核验结论的 authority
        ↓
ai_research_execution_adapter           唯一同时认识两套词表的 production 模块
        ↓
ai_research_contract                    research core；继续**不** import execution
```

**为什么不在契约里加一行 import。** `execution_verification` 同时包含 owner fact contract
（纯 value type）与 SQLite 读路径 / 回填 / 闸门谓词。让 research domain contract 直接
import 它，等于把手写研究契约绑到 execution 的 DB 读实现上；反过来让
`execution_verification` import research 又会反转依赖方向（AIG-02 / RG-04 已禁止）。
这是 roadmap **本身要求**的真实接缝，不是 wrapper 债：调用链上没有多出一个只转发的中间层，
而是把一条原本不可表达的能力变成可表达。

已由可执行断言锁定：`EXEC-REF-19` 断言三条 import 方向 + "同时认识两套词表的 production
模块恰好只有 adapter 一个"（非空性检查在同一个用例里）。

---

## Execution owner semantics

execution owner 的四态（`EXECUTION_STATUSES`）与证据来源（`EVIDENCE_SOURCES`）是**它自己的**
闭集。本 PR **不**复制、**不**翻译、**不**把任何状态词读成 market 词：

```text
partial        不会变成  single_source
unknown        不会变成  unavailable
not_executed   不会变成  not_attempted
```

`OwnerVerification.status` 逐字保留 owner 的状态词；research core 只消费三态 `outcome`。

---

## Status × source → OwnerVerification mapping / Partial / not_executed 区分

**本 PR 最关键的一点。** execution 的 `verification["is_verified"]` 回答
"**整张订单**是否被证明完整成交"，而 `OwnerVerification.is_verified` 回答"这个 owner-native
结论是否可以作为 research fact 被依赖"。**这不是同一个问题**，因此**禁止**：

```python
outcome = (OWNER_OUTCOME_VERIFIED
           if projection.verification["is_verified"]
           else OWNER_OUTCOME_UNVERIFIED)     # ← 会把 partial / not_executed 降级
```

显式穷尽表（不是 catch-all）：

| execution status | source | research outcome | 含义 |
| --- | --- | --- | --- |
| `verified` | `paper_orders+paper_fills` | `verified` | 完整成交 |
| `partial` | `paper_orders+paper_fills` | `verified` | **可信的"部分成交"事实** |
| `not_executed` | `paper_orders+paper_fills` | `verified` | **可信的"确认未执行"事实** |
| `unknown` | `paper_orders+paper_fills` | `unverified` | 核验做了，但得不出明确 fact |
| 四种状态 | `evidence_inconsistent` | `source_unusable` | 证据本身自相矛盾 |
| `not_executed` / `unknown` | `legacy_row_without_fill_evidence` | `source_unusable` | 无流水可核的旧行 |
| `unknown` | `no_evidence_available` | `source_unusable` | 连证据对象都没有 |

```text
status = partial        execution is_verified = False   research is_verified = True
                        ⇔ 完整成交？NO；"部分成交"这个事实可信？YES
status = not_executed   execution is_verified = False   research is_verified = True
                        ⇔ 完整成交？NO；确认没有执行？YES
```

owner 原来的整单布尔位**不丢**，但换了一个准确的名字进入 research attributes：
**`execution_fully_verified`**。刻意不沿用 `is_verified` —— 否则同一份对象上会同时出现
`ref.is_verified = True` 与 `verification_attributes["is_verified"] = False`，而两者对
`partial` 本来就不同，极易被误读。

**来源级别 vs 事实级别必须分开。** `not_executed + legacy` 归 `source_unusable` **不是**
说"确认未执行"这个结论不可信，而是说**证据基础**是一条 owner 不愿背书的旧行。因此假设层给的
原因不同：来源不可用 → `evidence_unavailable`（去修数据）；`unknown + ledger` →
`evidence_not_verified`（这条事实还不够好）。

### 双向穷尽，owner 词表漂移即 fail closed

合法组合集合由 owner **公开的** `verification_contract(status, source)` 推导（遍历
`EXECUTION_STATUSES × EVIDENCE_SOURCES`），并断言映射与它**精确相等**：

```text
set(_EXECUTION_OUTCOME_BY_VERIFICATION) == _owner_legal_pairs()
```

刻意**不** import owner 的私有 `_LEGAL_SOURCES_BY_STATUS`，也刻意**不缓存** —— owner 增删
状态 / 来源 / 合法组合时，adapter 在**下一次调用**就 fail closed，而不是等进程重启。
与 B2C-2 的 `_MARKET_OUTCOME_BY_VERIFICATION` 同一手法。`EXEC-REF-12` 直接测两个方向，并
**模拟 owner 侧契约变更**（patch owner 自己的词表与组合表）验证行为面 fail closed。

`OwnerVerification.attributes` 只保留 owner 的**核验**维度
（`verification_scope` / `verification_version` / `verification_source` /
`execution_fully_verified`）；`identity_kind` / `order_id` / `business_day` / `observed_at` /
`lifecycle_state` / `fill_verdict` 属于 fact/detail，**不**塞进 verification
（`EXEC-REF-14` 断言）。

---

## PIT behavior

```text
ResearchEvidenceRef.as_of  ←  projection.business_day，且必须 is_known
```

unknown 时**拒绝签发**。禁止 fallback：

```text
created_at / today() / datetime.now() / observed_at 的日期 / order_time / 墙钟   → 全部不出现
```

`EXEC-REF-06` 除了断言被拒的形态，还断言一条**观测时点已知、业务日未知**的事实同样被拒 ——
这才真正区分"没去用 observed_at"，而不只是"恰好两个都缺"。

**已知且故意保留的缺口**：

```text
execution fact without owner business_day
    → NOT ADAPTABLE YET（owner data prerequisite 未满足）
    → owner data gap = OPEN
```

被拒 / 被撤等部分 execution facts 今天在 `paper_orders` 上没有 owner 记录的交易日列。这不是
删能力：`EXEC-REF-09` 用 owner **自己的**签发口补记业务日，断言同一条事实立刻可签发，并且
`relation=supports / contradicts / context` 三种关系都能正确作用于 hypothesis。

---

## Identity derivation

```text
source_id  ←  <identity_kind>|<identity>      完全由 owner 投影派生
```

例如 `fill_event_key|<sha256>` / `fill_event_key_set|k1|k2` / `order_id_only|order:123` /
`fill_event_key_incomplete|order:123`。

adapter **不**重算 event key、**不**接受 `order_id`：identity authority 是 execution owner，
本层只把 owner 的身份编码成一个 research 引用，不重新定义 execution identity。
**fact contract 版本刻意不进入 identity** —— 版本升级不应该把同一条事实变成另一条事实
（`EXEC-REF-04` 断言）。

---

## Conflict fingerprint

`detail["content_fingerprint"]` 覆盖 factual projection（version / identity_kind / order_id /
lifecycle_state / fill_verdict / business_day / observed_at / inconsistencies），使用

```python
json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
→ sha256
```

刻意**不**用 `hash()`（Python 进程间不稳定）/ `repr(object)` / 内存地址 / 当前时间 / 随机数。
verification statement 不重复进指纹：它已经由 `OwnerVerification.canonical()` 单独进入
`fact_state`。

```text
同 identity + 内容变了（filled ↔ partially_filled 的等价流水被改写） → EvidenceConflict
同 identity + 内容相同                                            → 安全去重
```

`EXEC-REF-15` / `EXEC-REF-16` 双向断言，且**与输入顺序无关**。

---

## Issuance boundary

私有签发口的调用者从 B2C-2 的"契约外零调用"升级成**精确 allowlist**：

```text
ai_research_contract.py            → evidence_ref_from_market_reading
ai_research_execution_adapter.py   → evidence_ref_from_execution_projection
除此之外 0 calls
```

只做 **module + enclosing function** 结构扫描 —— 不实现 reaching definition / dominance /
CFG / branch tracking / try-flow 分析。`EVIDENCE-03` 用等值断言并要求 allowlist 里每一对都
真的在调用它（非空性）。

同时 owner registry 从 `{owner: factory}` 升级成 `{owner: (module, factory)}`，授权判据是
**(module, symbol) 对**且该模块必须**真的导出**该符号：

```text
本地同名函数                                  → 拒绝
其它对象的同名方法                            → 拒绝
其它（未登记）模块的同名工厂                   → 拒绝
已登记模块借用另一个 owner 的工厂名            → 拒绝（B2C-3 新增）
```

`SUPPORTED_OWNER_ADAPTERS == EXPECTED_OWNER_FACTORIES.keys()` 双向等值。契约自己导出的
factory 仍然只有 market 一份（execution 的那一份住在 adapter 模块），契约仍然不 import
execution。

adapter 的签名里**只有** `projection`：调用方不能提供 `source_id` / `as_of` /
`verification` / `outcome` / `business_day` / `verification_source`（`EXEC-REF-17`）。
输入类型用 `type(projection) is ExecutionFactProjection` 校验：dict / `Mapping` /
duck-typed 对象（含自带 `projection()` 方法的对象）/ 子类**全部拒绝**（`EXEC-REF-02`）。

---

## Architecture Impact

```text
Roadmap capability removed:                    0
Roadmap invariant weakened:                    0

Production modules added:                      1
    ai_research_execution_adapter.py
Production modules removed:                    0

New abstraction layers:                        1 real adapter boundary
New service:                                   0
New manager:                                   0
New repository:                                0
New facade:                                    0
Pass-through wrappers added:                   0

Supported research owners:   before = market_data
                             after  = market_data + execution

Execution verification authority:  before = execution_verification
                                   after  = execution_verification（未迁移）
Research execution verification authority:     0
Research-owned execution status semantics:     0
Execution → market vocabulary translation:     0

Runtime migrations:                            0
InformationEvent production constructors:      unchanged（仍恰好一处：deepseek_advisor）
Deepseek / provider / frontend changes:        0

Modules required to understand
execution → research evidence:
    before = capability absent
    after  = execution_verification
             ai_research_execution_adapter
             ai_research_contract

Net architecture surface:                      INCREASED
```

**`INCREASED` 是合理的，也是本轮唯一可以接受的方向**：新增的是原路线 B2C-3 **本身要求**的
owner boundary，不是 forwarding layer。它隔离 execution owner 与 generic research contract，
从而避免 `ai_research_contract` 直接依赖带 DB / read helpers 的 `execution_verification`。

---

## Maintainability Impact

```text
execution status → research outcome 映射          只有 adapter 一份
execution status/source 合法性                    仍只有 execution owner 一份定义
research hypothesis 层出现 EXECUTION_STATUS_* / EVIDENCE_SOURCE_* / MDC.VERIFICATION_*
                                                  0
ai_research_contract import execution_verification  NO
execution_verification import ai_research_contract  NO
同时认识两套词表的 production 模块                  1（adapter）
registry framework / BaseAdapter / manager / service / repository / facade
                                                  0
```

`EXEC-REF-20` 把这些变成可执行断言（含"映射实现只有 adapter 一份"、"owner 合法组合没有被
第二处重算"、以及"hypothesis 判定层不得出现任何 owner 核验词表"）。

---

## Tests

新增 `backend/test_ai_research_execution_adapter.py`（20 个用例）：

```text
EXEC-REF-01  真 ExecutionFactProjection 可以签发 ResearchEvidenceRef
EXEC-REF-02  dict / Mapping / fake / duck-typed(projection()) / 子类 全部拒绝（含非空性对照）
EXEC-REF-03  source_type == execution；InformationEvent.kind == execution_observed；kind 无可传通道
EXEC-REF-04  source_id 完全由 owner identity_kind + identity 派生（四种 kind 全覆盖）；版本不进入 identity
EXEC-REF-05  business_day known → as_of 正确；observed_at 只进 detail
EXEC-REF-06  business_day unknown → fail closed；**观测时点已知时仍拒绝**（证明没有 fallback）
EXEC-REF-07  verified + ledger → outcome verified；execution_fully_verified True
EXEC-REF-08  partial + ledger → outcome verified（**永久回归**）+ execution_fully_verified False
             + relation=supports 作用于 hypothesis → supported
EXEC-REF-09  not_executed + ledger → outcome verified（**永久回归**）+ execution_fully_verified False
             + 三种 relation 都能正确作用；被拒事实业务日 unknown → 拒绝（缺口可见）
EXEC-REF-10  unknown + ledger → unverified；relation=supports 无法升级；reason = evidence_not_verified
EXEC-REF-11  inconsistent / legacy / absent 的**每个**合法 pair → source_unusable
             + 行为面 reason = evidence_unavailable（与 evidence_not_verified 分开）
EXEC-REF-12  映射与 owner 公开契约**双向**精确相等；两个方向的纯函数检查都能红；
             模拟 owner 新增/收回组合 → 行为面 fail closed；非空性对照
EXEC-REF-13  非 market 事实 verification_method None / cross_source_verified False（含 event 投影）
EXEC-REF-14  owner attributes 深冻结 + 深冻结键集合精确 + fact metadata 不在其中
EXEC-REF-15  同 identity + 内容变了 → EvidenceConflict（与顺序无关）
EXEC-REF-16  同 identity + 同内容 → 安全去重；指纹确定性（不受 hash 随机化影响）
EXEC-REF-17  签名里只有 projection；仍无公开 raw 构造器；无构造哨兵
EXEC-REF-18  owner-origin provenance 仍 OPEN / REQUIRED，且**两步伪造路径仍可复现**
EXEC-REF-19  依赖方向单向；同时认识两套词表的模块恰好只有 adapter 一个
EXEC-REF-20  execution 词表只出现在 adapter；映射只有一份；无 registry framework
```

`backend/test_ai_research_evidence_ownership_guard.py`（19 个用例）：EVIDENCE-03 升级为
issuer caller 精确 allowlist 等值；新增"已登记模块不得借用另一个 owner 的工厂名"、
"同一模块里的第二个签发 helper"、"嵌套函数按最近 enclosing function 记账"等反例组。

同步更新（既有 guard 的**有意识的登记变更**，不是放宽）：

```text
backend/test_ai_research_contract.py        RVERIFY-10（registry 形状 + 契约不 import execution）
                                            AIG-02 / AIG-03（adapter 作为第四个已登记 AI 消费者）
backend/test_ai_provider_transport.py       RG-03 / RG-04 / RG-05（adapter 登记 + 不联网）
```

---

## Mutation

`work/r27b2c3_execution_adapter_mutation_check.py`：

```text
M-EXECREF-1   partial + ledger 被映射成 unverified                        → CAUGHT
M-EXECREF-2   not_executed + ledger 被映射成 unverified                    → CAUGHT
M-EXECREF-3   unknown + ledger 被升级成 verified                           → CAUGHT
M-EXECREF-4   evidence_inconsistent 被当成 verified，而非 source_unusable    → CAUGHT
M-EXECREF-5   business_day unknown 时 fallback 到 observed_at 的日期        → CAUGHT
M-EXECREF-6   source_id 不再包含 owner identity_kind / identity             → CAUGHT
M-EXECREF-7   content fingerprint 被常量化                                  → CAUGHT
M-EXECREF-8   adapter 接受 duck-typed projection                           → CAUGHT
M-EXECREF-9   status × source 映射改成 catch-all                            → CAUGHT
M-EXECREF-10  出现第三个未批准的 private issuer caller                      → CAUGHT
M-EXECREF-11  取消与 owner 契约的双向穷尽检查                                → CAUGHT

11/11 CAUGHT; survived = 0; fake = 0; restore sha256 = PASS
```

`SyntaxError` / `ImportError` / `NameError` 一律计为 FAKE（不计入 caught），逐次唯一
`PYTHONCACHEPREFIX`，anchor 唯一性有断言，跑完按启动快照 byte-identical 还原并校验 sha256。

---

## Full suite

```text
L0  ruff check backend                    PASS（无未登记 import / 未使用变量）
    compileall -q backend                 PASS
L1  execution_adapter + contract + execution_fact_contract            PASS
L2  + ownership_guard + provider_transport + research_service
    + research_repository + execution_fact_contract                   PASS（237 tests）
    另有 ruff check / compileall 定向覆盖四个改动文件                   PASS
    Python 3.11 与 3.12 本地各跑一次新增/相关 suite                     PASS
L3  python -m unittest discover -s backend -p "test_*.py"
    4417 tests, OK (skipped=5)                                        PASS
```

合并 authority 仍是**当前 PR HEAD 的 exact-head GitHub CI**（`tests (3.11)` / `tests (3.12)` /
`quality` / `syntax` / `docker-smoke` / `frontend` / `browser-e2e (chromium)` /
`security-leak-scan`）。按 `ARCHITECTURE.md` 的约定，本 PR **不**记录 HEAD sha 快照。

---

## Known provenance limitation

**owner-origin provenance = OPEN / REQUIRED，本 PR 没有关闭它，也没有宣称关闭。**

第 1 层（contract-issued boundary）本 PR **加强**了（issuer caller allowlist、`(module,
symbol)` 授权）。第 2 层仍是 OPEN：

```text
手工造 ExecutionEvidence → fact_projection(...)
    → evidence_ref_from_execution_projection(...) → 得到一个 ResearchEvidenceRef
```

今天仍然成立，与 `MarketDataReading` 在 R27-A 的情况相同。adapter 关闭的是"调用方不能自述
身份 / 业务日 / 核验结论"，**不是**"输入对象确实由 owner 产生"。`EXEC-REF-18` 把这条限制
断言下来（含模块 docstring 必须写清 OPEN / REQUIRED），而不是假装已封堵；也不引入
`issued=True` / 私有哨兵 / factory registry framework 这类只提供虚假安全感的形式主义。

另一条**继续 OPEN** 的 owner data gap：没有 owner-recorded `business_day` 的 execution 事实
（被拒 / 被撤）今天**不能**进入 research。这是 owner 侧的数据前置条件，不是可以靠 adapter
绕过的问题。

---

## Deferred B2C-4

```text
R27-B2C-4  migrate first execution-backed research runtime，优先 pnl_attribution
           （本 PR 的生产代码里 execution factory 的调用点 = 0，因此 B2C-4 是纯接线）
R27-B2C-5  news owner readiness
R27-B2C-6  adaptive / experiment owner readiness
R27-B2C-7  runtime / incident owner readiness
R27-B2C-8  remaining deepseek_research typed convergence
R27-B2C-9  ai_analysis lifecycle convergence
R27-B3     canonical research API/UI + 删除 B2B 兼容投影
```

**本 PR 之后不要自动进入 B2C-4。** 路线不删减。

---

## Docs

```text
ARCHITECTURE.md                       新增 "execution fact adapter（R27-B2C-3）" 一节；
                                      更新 SUPPORTED_OWNER_ADAPTERS、第 1 层边界、
                                      第 2 层 provenance、CI hard-fail 列表
docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md 新增 "B2C-3 进展"；B2C-1/2/3 COMPLETE、
                                      B2C-4 NOT STARTED；记录 partial / not_executed
                                      语义、PIT 缺口、identity、指纹、issuer allowlist、
                                      以及 B2C-3 之后的 maintainability 数字
```
