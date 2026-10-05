## R35-A only — Strategy Candidate Contract + Constrained Generator + Candidate Ledger

```text
R34 COMPLETE
R35 IN PROGRESS
R36 NOT STARTED
R37 NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```

R35 正式进入 **AI-assisted Strategy Evolution**，但 R35-A 的唯一核心权限是
`produce StrategyCandidate`。本 PR 不执行策略、不晋级策略、不覆盖 active strategy。

---

## 1. 核心链路（本 PR 只到候选台账为止）

```text
Research Hypothesis
        ↓
Generator Input
        ↓
Constrained Generator
        ↓
StrategyCandidate
        ↓
Candidate Ledger
        ↓
R36 Experiment Search / Validation
```

严禁 `Generator → Execution`，也严禁 `Generator → 直接覆盖 active strategy`。

## 2. 新增的四个 capability（无 wrapper / helper / facade 层）

| 模块 | 业务职责 | 负责的事实 | 依赖谁 |
| --- | --- | --- | --- |
| `strategy_candidate.py` | candidate **是什么**（纯契约） | canonical identity / fingerprint / 输入校验 | stdlib + `strategy_dsl_schema` + `strategy_parameter_schema` |
| `strategy_generator.py` | 从显式输入**生成**候选（纯边界） | generator 语义、受约束变体展开 | stdlib + `strategy_candidate` + 既有 DSL/参数契约 |
| `strategy_candidate_repository.py` | 候选台账**怎么存**（append-only） | candidate 行 + 提案证据 | stdlib + `strategy_candidate` |
| `strategy_candidate_service.py` | exact pin + **编排** | parent pin 的建立与追加 | registry + 上述三者 |

调用深度：API → service → (generator → candidate) + repository。

## 3. candidate identity ≠ candidate evaluation result

台账与契约里**没有** Sharpe / 收益率 / 回撤 / 胜率 / promotion 结果这些列或字段；
`FORBIDDEN_EVALUATION_KEYS` 出现即 fail closed。这些属于 R36 / R31 的事实。

`created_at` 是持久化元数据，**不属于** candidate 指纹，因此由台账单独发布
（`persistence.created_at`），前端要显示"何时记录"时读它。

## 4. immutable + canonical fingerprint

- `candidate_id` **就是** canonical fingerprint，schema 层再由
  `CHECK(candidate_id = candidate_fingerprint)` 钉一次。
- `no_update` / `no_delete` trigger：改动任何语义事实产生的是**另一个** candidate。
- canonicalize（`sort_keys` + 紧凑分隔符）后再 hash；不是 `hash(str(dict))`。
- 改变指纹：parent version / checksum、factor / entry / exit、参数值、DSL schema
  version、generator 语义版本、constraints、universe、intended regime。
- 不改变指纹：DB row id、显示名称、UI 排序、无业务意义的 JSON key 顺序。

## 5. parent strategy 必须 pin

只记 `strategy_id` 不够：那样未来会从 current registry 重新解释"当时用的是哪一版"。
本 PR 用 `strategy_id` + immutable `version` + `checksum` 三者同时钉住，并且
`strategy_candidate_service.pin_parent_strategy` 按 **exact version + checksum** 读
registry（checksum 不符由 registry 自己 raise），**绝不**用 head 补齐。

测试证明：parent v1 → candidate C；后来 parent 升级到 v2；重新读取 C 仍绑定 v1。

## 6. 受约束表示（复用既有 owner，不发明第二套语言）

- 表达式：`strategy_dsl_schema` 的封闭 op 集。任意可执行 payload
  （`python` / `eval` / `exec` / shell / 动态 import / 属性访问）fail closed，
  原因是**没有那个 op**，不是子串过滤。
- 参数：`strategy_parameter_schema`（allowlist / bounds / `max_step` / locked /
  `min_evidence`）。generator 只能取调用方显式声明的值，且每个值仍要过父策略自己的
  参数契约。
- 结构不可变：generator 只改**已声明可调参数的值**，绝不增删条件、改算子或改字段引用。

## 7. Generator 边界

`GeneratorInput` 显式携带全部事实：parent pin、as-of、universe、intended regime、
参数调整声明、evidence_count、hypothesis / provenance、random_seed、model identity。
generator 内部**没有** DB、registry、`datetime.now()` 业务 as-of、current/latest 读取。
缺任一必需事实即 fail closed，绝不偷偷用 latest 补齐。

## 8. Dedup authority

唯一权威是 canonical candidate fingerprint。同一个候选再次被提出：身份不变、行数不增；
每次提案的来源证据（generator / as-of / hypothesis / model / input fingerprint）追加进
`strategy_candidate_proposals`。禁止以名字相同 / 描述相似 / LLM 文本相似 / 创建时间相近
作为去重依据。

## 9. Persistence

migration **v33**，DDL 唯一 owner 是
`paper_schema_migrations.ensure_strategy_candidates`（`db_migrate` 与 `init_db` 快路径
调用同一个函数）。业务代码运行时**不**做 `CREATE TABLE` / `ALTER TABLE`。
两张 append-only 表：`strategy_candidates`（15 列，**无评估列**）与
`strategy_candidate_proposals`。**不回填**：升级前不存在"候选"这个事实。

## 10. HTTP 与前端

- `POST /api/strategies/{id}/candidates`（201）
- `GET /api/strategies/{id}/candidates?strategy_version=&strategy_checksum=`（按 exact pin）
- `GET /api/strategies/{id}/candidates/{candidate_id}`（只按显式 id；无 latest 路由）

前端只在策略工坊详情页提供**最小只读**候选台账：Candidate ID、parent strategy/version/
checksum、generator type/version、created_at / asof、fingerprint、specification 摘要、
`status = CANDIDATE`。前端不判断候选是否优秀 / 能否晋级 / 是否允许进 Shadow
（后端在这一层明确不发布 `evaluation` / `promotion`，两者都是 `null`）。

## 11. 没有改动正式策略

`tq_breakout` / `main_force_top10` 等正式策略的交易规则、入场阈值、退出阈值、风控、
allocation、execution、portfolio weights **全部未改**；现有策略只作为
pinned baseline / parent / reference 存在。本 PR 的验收标准是**可信的生成基础设施**，
不是"证明某个新策略赚钱"——因此没有任何以收益 / Sharpe / 胜率作为验收条件的断言。

---

## Review feedback addressed (P2 × 2)

| Finding | Fix | Regression |
| --- | --- | --- |
| P2 — parent constraints dropped when no override is supplied (`strategy_candidate_service.py`) | `pin_parent_strategy` now inherits `definition.metadata.constraints` from the **exact pinned version** when the caller supplies no override. An empty constraint set is not "unconstrained": it silently discarded the parent's position / exposure / weight limits and made the candidate no longer the parent's semantics. An explicit override still replaces it. | `test_c3e_parent_metadata_constraints_are_inherited_not_dropped` |
| P2 — same-second proposals collide and lose an event (`strategy_candidate_repository.py`) | `proposal_id` is now an **opaque event identity** (`secrets.token_hex(32)`), not a content fingerprint: it no longer depends on a process-local counter, PID, thread id, wall-clock timestamp, or any candidate/proposal content hash as its uniqueness authority. Two proposals of the same candidate with the same input and an **identical `created_at`** are two distinct append-only rows. The event write is a fail-closed plain `INSERT` (no `INSERT OR IGNORE`), so an unexpected id collision raises instead of pretending the second event was recorded. `created_at` stays event timestamp / ordering metadata only. | `test_c6d_every_proposal_occurrence_gets_its_own_identity`; `test_c6e_proposal_event_identity_survives_process_local_identity_reset`; `test_c6f_unexpected_proposal_id_collision_fails_closed`; `test_c6g_proposal_identity_is_not_a_content_fingerprint` |

Both were reproduced before fixing (same-second proposals collapsed to 1 row;
inherited constraints came back as `{}`).

## Candidate identity vs proposal event identity

```text
candidate row  → content identity: canonical fingerprint → semantic dedup (idempotent)
proposal row   → event identity:   opaque event id      → append every occurrence
```

`proposal_id` is an opaque event id, **not** a proposal content fingerprint. Forbidden
as uniqueness authority: process-local counter, PID, thread id, second-level (or any)
wall-clock timestamp, candidate content hash, proposal content hash. `created_at` is
metadata, not uniqueness authority. Candidate rows keep `INSERT OR IGNORE` (the
canonical fingerprint **is** the dedup authority); the event table uses a fail-closed
`INSERT`. The append-only `*_no_update` / `*_no_delete` triggers are unchanged.

## Focused tests

```text
backend/test_r35a_strategy_candidate.py        51 tests  OK   (C1–C10)
  incl. C6d / C6e / C6f / C6g (proposal event identity, fail-closed collision)
backend/test_strategy_api_contract.py / test_db_migrate.py
  / test_paper_schema_migrations.py / test_strategy_dsl.py
  / test_strategy_parameter_schema.py / test_strategy_registry.py
  / test_repository_hygiene.py                 110 tests OK (skipped=1, focused set)
frontend/tests/strategy-candidates.test.mjs     6 tests  pass (R35A-C1…C6)
ruff check backend / compileall / git diff --check   all clean
```

C1 deterministic identity · C2 semantic mutation changes identity · C3 parent pinning ·
C4 arbitrary executable payload rejected · C5 missing provenance fails closed ·
C6 candidate semantic dedup + proposal event identity (C6d/C6e/C6f/C6g) ·
C7 append-only identity · C8 persistence round trip · C9 no promotion/execution
authority · C10 current-state leakage cannot rebind a stored candidate.

## Mutation result

`work/r35a_candidate_mutation_check.py`:

```text
M-G1 DETECTED (candidate provenance re-resolves the parent from the current head)
M-G2 DETECTED (the canonical fingerprint ignores parameter values entirely)
M-G3 DETECTED (candidate identity becomes random instead of canonical)
M-G4 DETECTED (a missing parent checksum falls back to the current version)
M-G5 DETECTED (arbitrary executable candidate payload is accepted)
M-G6 DETECTED (a parameter-only variant silently drops the parent's constraints)
M-G7 DETECTED (proposal identity degrades to candidate + payload + timestamp and loses an event)
M-G8 DETECTED (a proposal id collision is silently swallowed by INSERT OR IGNORE)
M-G detected = 8/8
survived = 0; fake = 0; timeout = 0
restore SHA256 = PASS
baseline after restore = GREEN
```

## Full verification

```text
backend full suite (local, Python 3.14.5)      Ran 5346 tests   OK (skipped=5)   [previous head]
backend full suite (Docker, --network none)    Ran 5346 tests   OK (skipped=30)  [previous head]
frontend unit tests (node --test)              163 tests  pass 0 fail
Chromium E2E (npx playwright test, workers=1)  37 passed
ruff check backend                             All checks passed!
python -m compileall -q backend                clean
git diff --check                               clean
security leak scan (--scope worktree / all)    kinds none, values 0, exit 0
```

本 head 的 focused 验证（P2 proposal-identity 修复）见上；完整 gate 交给 exact-head
GitHub CI（tests / docker --network none / frontend / Chromium E2E / quality / syntax /
security）。

## Architecture / maintainability report

```text
Business authority added:
  strategy_candidate        = canonical StrategyCandidate identity + fingerprint
  strategy_generator        = GeneratorInput -> StrategyCandidate (pure)
  strategy_candidate_repository = append-only candidate ledger
  strategy_candidate_service    = exact parent pin + orchestration
  paper_schema_migrations.ensure_strategy_candidates = candidate DDL (migration v33)

Business authority removed: none

Candidate authority:     strategy_candidate (pure contract)
Fingerprint authority:   strategy_candidate (canonicalize + sha256)
Dedup authority:         strategy_candidate fingerprint (唯一)
Parent strategy authority: strategy_registry (exact immutable version + checksum)
                         service 只 pin，不复制第二套 strategy identity/version/checksum resolver

Duplicate implementation removed: NO (none existed)
New facade/wrapper:   0
Old facade/wrapper removed: 0
Implicit current-state lookup added: 0
Direct DB write sites: before = 0 / after = 0 (candidate tables only; no formal ledger write)
Large strategy if/elif chain added: NO
Frontend duplicated business rule: NO
Arbitrary generated Python execution path: NO
Generator -> lifecycle mutation dependency: NO
Generator -> execution dependency: NO

理解 StrategyCandidate 核心规则需要查看：
before = n/a (contract is new)
after  = 1 module (strategy_candidate.py)

paper_trading.py: LOC 15780 / defs 294（仅趋势观察，不作为 blocker；本 PR 只加了
init_db 快路径的一行 schema 调用）
```

## Explicitly NOT in R35-A

Bayesian optimisation、evolutionary population search、大规模参数搜索、自动 walk-forward、
自动 PIT backtest queue、自动 robustness queue、AI 自动修改生产策略、自动 Shadow / Paper /
Production-Sim promotion、Learning Store feedback loop。这些属于 R35-B/C、R36、R37。

完成后停止，等待人工审核。**不自动合并，不开始 R35-B。**
