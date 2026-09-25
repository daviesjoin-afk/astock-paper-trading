# R27-B2C-5 —— news owner readiness

把 news/event ledger 的事实边界整理成 owner 签发的 typed evidence，让 AI research contract
能够**安全消费** news owner 的 typed fact。本轮是 **owner readiness**，**不是** event_evidence
runtime convergence。

```text
news_learning durable event ledger          （owner：身份 / PIT / 核验结论的 authority）
        ↓
NewsFactProjection                          （owner typed fact projection）
        ↓
OWNER_OUTCOME_UNVERIFIED / SOURCE_UNUSABLE  （owner-native 核验归口，**没有** verified）
        ↓
ai_research_news_adapter
        ↓
ARC.ResearchEvidenceRef(source_type="news")
```

**本轮刻意不做的事**：不删 `deepseek_research._event_evidence()`、不改 deepseek runtime、
不消灭 live fetch fallback。于是 news adapter 的 production 调用点是 **0** —— 这是 B2C-5
的预期状态，不是缺陷。

## 目标 / 非目标

| | |
| --- | --- |
| 目标 | owner 发布 typed news 事实；research 侧有唯一 adapter 能读懂它；两侧都有结构回归与 mutation 证据 |
| 非目标 | `_event_evidence` runtime 迁移（后续 event_evidence convergence）；新增 DB schema；改动 ingestion writer 的抓取行为 |

## NEWS OWNER MATRIX（摘要，完整表见 `docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md`）

| table / fact | canonical owner | PIT availability | 本轮 treatment |
| --- | --- | --- | --- |
| `news_events` | `news_learning` | **`first_seen_at`** | TYPED EVENT FACT |
| `market_major_events` | `news_learning` | **`first_seen_at`** | TYPED EVENT FACT |
| `market_event_candidate_links` | `news_learning` | `created_at` | PRESENTATION / CONTEXT（不是 event truth） |
| `news_source_reputation` | `news_learning` | 无可变快照 as-of | OWNER METADATA（不是 per-event verification） |
| `news_event_outcomes` / `news_effectiveness` | `news_learning` | 派生 | DERIVED LEARNING METADATA |
| `news_factor_versions` | `news_learning` | `created_at` | OWNER METADATA / OUT OF EVENT EVIDENCE SCOPE |
| `news_learning_runs` / `news_candidate_snapshots` | `news_learning` | — | OUT OF B2C-5 SCOPE |
| fetch 端 `"verified": True` | 无（落库时被丢弃） | — | NOT VERIFICATION |

## 核心不变量

1. **`first_seen_at` 是唯一的 historical availability authority。**
   `published_at`（来源声称的发布时间）与 `created_at`（行写入时刻）在任何方向上都不得改变
   可用性。`published=9/20`、`first_seen=9/21` 的事件在 `as_of=9/20` **必须不可见**。
2. **PIT 不可证明时 fail closed。** 缺失 / 畸形 / naive 的 `first_seen_at` 一律拒绝，
   **没有** `published_at` / `created_at` / `now()` fallback。
3. **typed read 不联网、不写库。** 只发 SELECT、不建表；ledger 不可读时 fail closed 成
   `ledger_unavailable`，**不**触发 ingestion fetch / backfill。这条边界是**不对称**的：
   ingestion writer 允许联网，typed owner read 与 adapter 不允许。
4. **`evidence_grade` 不是核验。** A/B/C/D 是来源可追溯性分级；`grade="A"` 不得升级任何结论。
5. **单源可追溯不是核验。** `single_source_linked` 与 `unverified` 都归
   `OWNER_OUTCOME_UNVERIFIED`；不可追溯归 `OWNER_OUTCOME_SOURCE_UNUSABLE`。
6. **candidate-link confidence / source reputation 不是事件核验。** 两张表**不在读路径里**，
   projection 不携带任何 confidence / score 字段 —— 结构上不可表达。
7. **research 只通过一条 typed adapter 消费 news。** adapter 只接受真正的
   `NewsFactProjection`（`type(...) is`），签名只有 `projection`，且不 import DB / 网络 / 时钟。
8. **未知 ledger 状态 hard error。** `verification_status` 的合法值是**审计产物**：回归直接
   扫描 writer 源码，要求字面量与 owner 登记逐字一致，不得 `else: unverified`。
9. **`CURRENT NEWS OWNER HAS NO VERIFIED STATE`** —— 审计事实：全仓只有一处
   `verification_status` writer，只写得出两个值，没有任何 UPDATE / 第二 writer / 多源复算
   路径能升级；`news_events` 连核验列都没有。因此 owner 闭集刻意没有 verified，
   归口表刻意不产生 `OWNER_OUTCOME_VERIFIED`。
10. **legacy news runtime 明确保持 OPEN**，直到后续 convergence。

## Architecture Impact

```text
Production modules added:                 1 maximum —— ai_research_news_adapter.py
Production modules removed:               0
New abstraction layers:                   0
Pass-through wrappers added:              0
Compatibility paths added:                0
DB migration / schema change:             0（沿用既有 event_key / first_seen_at /
                                          evidence_grade / verification_status）
New service / manager / repository /
facade / registry framework / BaseAdapter: 0

Capability owners before:
    news_learning durable ledger exists
    but no approved typed research seam

Capability owners after:
    news_learning remains sole news fact owner
    research consumes typed projection through one adapter

Runtime migration:                        NO
Legacy event_evidence path removed:       NO —— explicitly deferred
Roadmap capabilities preserved:           YES
Roadmap capabilities advanced:            typed news owner readiness
Original invariants weakened:             NO
Net architecture surface:                 INCREASED（有理由）
```

**为什么是 INCREASED 而不是 NEUTRAL**：增大的那一份是 roadmap 明确要求的
owner→research **缺失接缝**。`ai_research_contract` 不得 import DB-backed 且会在写路径联网的
`news_learning`；news owner 也不得 import research。这条接缝必须存在，而且只能有**一条**。
它不是新增第二个 news owner，也不是 forwarding layer。

依赖方向（单向）：

```text
news_learning (owner)
        ↓
ai_research_news_adapter (唯一认识两套词表的地方)
        ↓
ai_research_contract (不 import 任何 owner / DB 模块)
```

## Maintainability Impact

```text
news factual owners:                     before = 1        after = 1（未迁移）
news typed fact contract:                before = 0        after = 1
news adapter count:                      before = 0        after = 1
news adapter production callers:         before = 0        after = 0（预期）
duplicate news ledgers:                  before = 0        after = 0
new modules:                             1（adapter）
new wrappers:                            0
typed projection 新增模块:                0（住在既有 news_learning.py）
network-capable typed read paths:        0
implicit historical time fallbacks:      0
deleted roadmap capability:              0
```

**已知限制（不声称已关闭）**：

```text
contract-issued news projection:                       CLOSED
caller self-declared identity / status / as_of:         CLOSED
physical database origin / trusted database provenance: OPEN / REQUIRED

OPEN / REQUIRED:
deepseek_research._event_evidence still contains legacy direct-ledger reads
and live-fetch fallback（没有 durable events 时抓 live news）。
该 fallback 不代表 canonical news evidence path。
event_evidence runtime convergence = DEFERRED
```

**一处被 mutation 逼出来的 production 修正**：`M-NEWS-04`（"未知 ledger 状态被静默默认"）
第一次跑出来是 `SURVIVED` —— `NewsFactProjection.__post_init__` 当时**重复**了一遍与
`_major_event_owner_status` 等价的闭集检查，把退化挡住了。两处等价的 fail-closed 互相掩盖，
使两条路径中的一条实际上不受回归保护。修正是**去掉重复**（闭集合法性只有一处判定，与
`paper_portfolio_read_model` 的"校验逻辑只有一份"一致），而不是给变异找借口。

## 变更清单

**production（3 个文件）**

- `backend/ai_research_news_adapter.py`（新增，唯一新 production module）：唯一公开
  `evidence_ref_from_news_projection(projection)` + 显式穷尽归口表 + 确定性内容指纹。
- `backend/news_learning.py`：新增 owner typed fact contract —— `NewsFactProjection` /
  `NewsFactContractError` / `news_fact_projections(conn, *, as_of, …)` /
  `NEWS_MAJOR_EVENT_WRITER_STATUSES` / `news_owner_status_mapping_problems`。
  **没有**改任何 writer、没有改 schema。
- `backend/ai_research_contract.py`：`SUPPORTED_OWNER_ADAPTERS` 加入 `news`；
  `_issue_evidence_ref` 的批准 caller 文档增加 news factory；`ResearchEvidenceRef` 的报错
  文案列出 news factory。

**guards 同步登记（4 个文件）**

- `test_ai_research_evidence_ownership_guard.py`：`EXPECTED_OWNER_FACTORIES` /
  `APPROVED_ISSUER_CALLERS` 各 +1 行（caller set 恰好四个 owner factory）。
- `test_ai_research_contract.py`：`ALLOWED_AI_CONSUMERS`（AIG-03）、AIG-02 反向 import
  元组、`SUPPORTED_OWNER_ADAPTERS` 等值断言、契约反向 import 元组。
- `test_ai_provider_transport.py`：`NETWORK_FREE_SEAMS` / RG-04 / RG-05 登记新接缝；
  RG-03b 的非空性证明改成对闭集里**每一条**接缝各做一次 in-memory mutation。
- `test_ai_research_service.py`：`LOWER_LAYER_MODULES` +1。

**回归与验证（3 个文件）**

- `backend/test_news_fact_contract.py`（新增）：NEWS-01 ~ 30（owner 侧）。
- `backend/test_ai_research_news_adapter.py`（新增）：NEWS-03 ~ 33（research 侧）。
- `work/r27b2c5_news_owner_mutation_check.py`（新增）：M-NEWS-01 ~ 17。

**文档（2 个文件）**

- `docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md`：新增 B2C-5 进展（含 NEWS OWNER MATRIX 与
  数字），并给 Family C 的旧结论加一条**复查更正**指向它（保留原文，不删依据）。
- `ARCHITECTURE.md`：新增 `### news owner readiness（R27-B2C-5）` 一节 +
  CI hard fail 列表新增 7 条 news 不变量。

## 验证

```text
L1  python --version                        3.14.5
    python -m compileall -q backend         PASS
    ruff check backend                      All checks passed（ruff 0.16.6，与 CI 同版本）
    git diff --check                        PASS

L2  focused
    test_news_fact_contract                 17 tests PASS
    test_ai_research_news_adapter           16 tests PASS
    test_ai_research_evidence_ownership_guard / test_ai_research_contract /
    test_ai_provider_transport / test_ai_research_service        PASS
    test_ai_research_execution_adapter / test_ai_research_portfolio_adapter
    test_portfolio_fact_contract / test_execution_fact_contract  PASS

L3  work/r27b2c5_news_owner_mutation_check.py
    baseline = GREEN（29 个永久回归目标先于 mutation 验证）
    detected = 17/17   survived = 0   fake = 0   timeout = 0
    restore sha256 = PASS（adapter / news_learning / contract 三个被改写文件）

L4  python -m unittest discover -s backend -p "test_*.py"   见 CI / 本地记录

CI  见 "GitHub Actions checks on current PR HEAD"（tests / syntax / quality /
    docker-smoke / frontend / browser-e2e (chromium) / security-leak-scan）
```

> 本 PR 不记录 `HEAD = <sha>` 快照：merge authority 是当前 PR HEAD 的 exact-head CI，
> 从 GitHub API 读取，而不是文档里写下的某个旧 SHA（避免"为更新 SHA 再 commit"的循环）。

## 刻意排除

- 不迁移 `deepseek_research._event_evidence()` 的 runtime；不删它的 direct-ledger 读取与
  live-fetch fallback。
- 不新增 `news_verification_status` 列、不新建 `news_fact` 表、不复制现有事件 ledger。
- 不给 `news_source_reputation` / `news_factor_versions` 发 `EVIDENCE_SOURCE_NEWS`。
- 不做 source trust model、不做 candidate-link 因果确认。
- 不开始 B2C-6（adaptive / experiment owner readiness）。

## PR 状态

```text
MERGE:  NOT MERGED（等待人工审核）
DEPLOY: NOT DEPLOYED
NEXT:   R27-B2C-6 仅在人工审核之后
STATUS: AWAITING HUMAN REVIEW
```
