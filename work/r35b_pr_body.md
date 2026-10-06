## R35-B only — Deterministic Candidate Expansion

```text
R34     COMPLETE
R35-A   COMPLETE（PR #228，merge commit 4c4c346）
R35     IN PROGRESS
R35-B   IN REVIEW
R35-C   NOT STARTED
R36     NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
```

exact base SHA：`4c4c346206dc0c83b3c0f03a4da4caf8b88128d4`
分支：`codex/r35b-deterministic-candidate-expansion`

R35-A 回答"StrategyCandidate 是什么"；R35-B 回答"系统能生成哪些**受约束策略候选**"，
仍然**不**回答"哪个候选赚钱 / 哪个最好 / 哪个该晋级"。

---

## 1. 核心链路

```text
Pinned Parent Strategy
        ↓
Explicit Candidate Search Space
        ↓
Bounded Deterministic Generators
        ↓
StrategyCandidate[]
        ↓
Candidate Ledger + Proposal / Batch provenance
```

严禁 `Generator → Backtest`、`Generator → Promotion`、`Generator → Execution`。

## 2. R35-B 只拥有四个权限

```text
candidate search-space declaration
candidate expansion
candidate generation
candidate proposal recording
```

不得拥有：backtest、PIT validation、robustness validation、candidate scoring、
candidate ranking、winner selection、promotion、lifecycle transition、Shadow / Paper /
Production-Sim activation、execution、portfolio allocation、risk override。

## 3. 新增的 capability（无 wrapper / helper / facade 层）

| 模块 | 业务职责（一句话） | 负责的事实 |
| --- | --- | --- |
| `strategy_candidate_search_space.py`（新） | **搜索空间是什么** | 显式/有限/可 fingerprint 的生成输入声明、组合基数、slot 继承语义 |
| `strategy_generator.py`（扩展） | **确定性候选展开** | capability registry（5 个能力）+ 同一个确定性笛卡尔展开 |
| `strategy_candidate.py`（v2） | candidate **内容**身份 | canonical fingerprint（generator 能力身份已移出） |
| `strategy_candidate_repository.py`（扩展） | 三条 append-only 台账 | candidate / proposal / generation batch |
| `strategy_candidate_service.py`（扩展） | exact pin + 空间组装 + batch 身份 + 追加 | 编排（无评估、无 lifecycle） |
| `paper_schema_migrations.py`（v34） | DDL 唯一 owner | 候选表 v2 重建 + batch 追加表 |

调用深度：API → service → (search space → generator → candidate) + repository。

## 4. 三种身份，三套契约

```text
candidate row  → content identity (canonical fingerprint)      → semantic dedup
proposal row   → opaque event identity (secrets.token_hex(32)) → append every occurrence
batch row      → opaque request identity                       → append every request
```

**generator 能力身份与提案 provenance 整体属于事件，不属于内容。** 移出 candidate
identity 的是两族事实：

1. generator 能力身份（`generator_type` / `generator_version` / `generator_contract_version`）——§16 要求 generator 语义版本可被观察，§18/B9 要求不同能力提出同一份 specification 必须 dedup；
2. 提案 provenance（`hypothesis_id` / `research_provenance` / `random_seed` / `model_identity`）——R35-C 接入 AI generator 后，GPT model A 与 model B 提出同一份策略必须得到同一个 `candidate_id`。

只搬走第一族、第二族留在内容里，等于把身份分裂的成因从一种换成另一种。

**这是形状级的 ownership 转移，不是 `fingerprint_material.pop()`**，并且贯穿到
persistence：

- v2 candidate 的**投影**与**内容身份**都不含这七项；
- `candidate_from_projection` 在 v2 形状里读到它们直接 `fail closed`（防止有人手工塞回 `candidate_json`）；
- `build_strategy_candidate` **不接受**这些参数（接受再丢弃同样会误导调用方）；
- v2 `strategy_candidates` **不含任何 generation provenance 列**（`hypothesis_id` /
  `random_seed` 已移除），`append_candidate()` 也不再写它们；
- `StrategyCandidate` 带 **v2 shape invariant**：`candidate_schema_version == v2` 时携带
  任何 generation provenance 都 fail closed，让构造器、读回、直接构造 / `replace()`
  三条入口守同一条契约；
- v1 历史行继续按自己的旧材料自证，不改历史。

为什么必须删到列这一层：留着会形成"``candidate_json`` 里没有、独立列里有"的两套互相
矛盾事实，而 `get_candidate()` 只读 `candidate_json` —— 那些隐藏值写进去就再也读不出来，
也清不掉（`INSERT OR IGNORE` 加幂等只比 json / fingerprint）。已实测：写入
`hypothesis_id="hidden-hyp", random_seed=123` 后，再用**干净** candidate 追加，这两列
依旧是隐藏值。

`StrategyCandidate` 的 v1 兼容字段（generator 三件套 + 四项提案 provenance）只服务
**历史行的往返重建与自证**，v2 行为空。

candidate schema 因此升级为 `strategy-candidate-v2`；migration **v34** 重建候选表
（forward-only、幂等、**不回填**），历史 v1 行的 `candidate_json` 逐字保留。重建条件是
"存在任何 v1 遗留列"，因此本开发分支上跑过早期 v34 的本地库所处的**中间形态**
（generator 列已去、`hypothesis_id` / `random_seed` 还在）也会走同一条路径，不会永久
卡在双表示。

## 4b. 读模型不制造隐式 latest

`list_candidates_for_parent()` 发布的是**全部**提案证据引用
（`proposal_evidence.proposals` / `proposal_count` / `generation_batch_ids`），
**不是**"最近一条 proposal"。原因：`proposal_id` 是随机 opaque id，两条事件可以合法
拥有完全相同的 `created_at`，`proposals[-1]` 只是一个稳定但语义错误的"latest"。要看某个
batch 的完整输入，用显式的 `GET .../candidate-generations/{batch_id}`。append-only
历史要么完整给出，要么按显式 id 单独取；不重新投影成隐含的 current 指针。

## 5. Generator 能力：显式 registry，不是 if/elif 链

| generator_type | 展开的维度 | 版本 |
| --- | --- | --- |
| `parameter_variant` | parameters | v1 |
| `factor_variant` | factor | v1 |
| `entry_variant` | entry | v1 |
| `exit_variant` | exit | v1 |
| `bounded_combination` | parameters × factor × entry × exit | v1 |

`GeneratorCapability.dimensions` 是**唯一**的能力语义声明；展开本身是同一个确定性
笛卡尔展开。新增能力不需要碰展开逻辑，也不可能出现"某个能力偷偷多展开一个维度"。
`test_b12f_generator_dispatch_is_a_registry_not_a_branching_chain` 用 AST 断言模块里
没有按能力名比较的 `if/elif` 链。

## 6. 有界：基数先算，超限 fail closed

`cardinality` 是显式声明的一部分，生成前就能算出来。超过
`MAX_CANDIDATES_PER_GENERATION_REQUEST`（**128**，契约上限，调用方只能收紧）一律拒绝。

**绝不静默截断**：截断会让 candidate universe 依赖遍历顺序。B3 刻意用 200 个**全部
合法**的 factor 备选做探测 —— 那样"截断"会返回 128 个候选、而契约要求直接报错，
两种实现才可区分（用非法组合做反例的话两种实现都抛异常，测不出东西）。同一个原则
还覆盖第二种形态：**能力不展开的维度必须只有一个取值**（否则"声明了 3 个 entry
备选、却只用了第 1 个"同样是静默截断），由 B3d 覆盖。

## 7. 组合顺序不影响 candidate 集合

JSON key 顺序 / 参数声明顺序 / factor / entry / exit alternative 顺序都不承载语义：
`_slot` 对 alternative 去重 + canonical 排序，参数值排序，输出按 `candidate_id`
canonical 排序，空间指纹 canonical。B2 / B2b / B2c 证明打乱声明顺序后
fingerprint set 完全一致。

## 8. 继承语义唯一（§11）

```text
INHERIT_PARENT    → 继承 exact pinned parent 那一版的最终语义
EXPLICIT_VARIANT  → 显式备选（每个都过 strategy_dsl_schema）
ABSENT            → 本候选明确没有这个角色
```

三者**互斥**，不允许用 `None` 同时表示三件事；`inherit_parent` 与 `absent` 都不接受
`alternatives`（否则同一份 JSON 有两种读法）；`entry` 不能是 `absent`。

继承在**生成阶段**就解析成明确值写进 candidate，绝不持久化
`"exit": "inherit-current-parent"` 然后未来回读 registry（那会重新引入
current-state leakage）。B7 证明：父策略升级到 v2 后，旧候选的语义与身份都不变。

## 9. constraints：inherit, or only tighten

默认 `candidate constraints = exact pinned parent constraints`；显式 override 只能
**收紧**。丢掉父策略已有的边界、或放宽任一上限（`max_positions` /
`max_exposure_pct` / `max_weight_pct`）一律 fail closed —— 放宽是风险放大动作，
必须走正式 risk evidence gate（`asymmetric_risk` 的观察期），不属于 candidate
generator 的权限。C3e 覆盖继承 / 收紧 / 放宽三类情形。

## 10. Generation batch provenance（§19/§20）

```text
一次生成请求 → 一个 batch identity → N 条 proposal 事件
```

`generation_input_fingerprint` 绑定 exact parent pin + search-space canonical
fingerprint + generator 能力/契约版本 + as-of + research provenance，因此"这一批候选
到底是从什么输入生成的"永远可回答，而不是只留一句"generated by R35-B"。

batch identity **不是** candidate identity 的一部分：同一个 candidate 可以出现在不同
batch（B11）；两个内容完全相同的请求也是两次独立的请求事件。batch 表 append-only、
无 `current_generation_batch` 指针，读路径只能按显式 batch id 取。

## 11. 结构变异限制（§13）

允许：在明确 slot 中替换 factor / entry / exit、在允许范围内改变 parameter value。
禁止：随机增删 AST 节点、随机反转比较符、随机替换 field / operator / constraint。
R35-B 不做自由 AST mutation，也没有 `while score improves: mutate()`、没有自动挑 best、
没有"根据 Sharpe 决定下一组参数"（全部留到 R36）。

## 12. 不要把策略研究知识写死进 generator（§22）

generator 里没有 `if market_regime == "bull"`、没有 `if strategy == "tq_breakout"`：
它负责组合 caller 给的合法空间，不负责决定牛市该用什么、哪个 factor 最赚钱。

## 13. Universe（§15）

universe 只是**显式声明 / identity**；generator 不读"今天可交易股票"、不读 current
watchlist。某历史日期能否交易仍由 Market Data / Tradability authority 判定，candidate
generator 不复制第二套 PIT tradability 规则。

## 14. API 与前端

扩展**已有** `POST /api/strategies/{id}/candidates`（不新增重复 authority endpoint），
新增只读 `GET /api/strategies/{id}/candidate-generations/{batch_id}`。HTTP 只做输入解析 /
typed request contract / 调用 application service / 错误映射；空间展开、基数计算与候选
构造都不在 route 里。

前端只增加最小可视化：generation batch、generator type、candidate count、search-space
summary、parent pin、as-of、候选列表。**没有** Best Candidate / Score / Winner /
Recommended / Promote / Deploy，也不按参数大小、候选数量或某些 rule 判断质量。

## 15. Persistence 与 migration（§25/§26）

```text
strategy_candidates                        （v2：内容身份，无 generator 列）
strategy_candidate_proposals               （+ generation_batch_id，事件 provenance）
strategy_candidate_generation_batches      （新：请求 provenance）
```

DDL 由 `paper_schema_migrations` 唯一持有（migration **v34**，forward-only、幂等、
不回填）；业务模块运行时不做 `CREATE TABLE` / `ALTER TABLE`（B12d 用 AST 断言）。
历史 proposal 事件没有 batch 归属，`generation_batch_id IS NULL` 就是诚实的 legacy
状态 —— 绝不给旧行凭空生成 batch。

## 16. 没有改动正式策略

`tq_breakout` / `main_force_top10` 等正式策略的 entry / exit / factor / 风险阈值 /
allocation / execution **全部未改**；所有优化都只能表现为 **new StrategyCandidate**，
绝不原地优化生产策略。

---

## 17. 测试矩阵（B1–B12）

| ID | 覆盖 |
|---|---|
| B1 | `test_b1_same_search_space_yields_the_same_candidate_set`; `test_b1b_search_space_fingerprint_is_canonical` |
| B2 | `test_b2_declaration_order_never_changes_the_candidate_set`; `test_b2b_reversed_mapping_keys_do_not_change_identity`; `test_b2c_parameter_declaration_order_never_changes_the_candidate_set` |
| B3 | `test_b3_oversized_combination_space_is_rejected_not_truncated`; `test_b3b_cardinality_is_computable_before_expansion`; `test_b3c_declared_budget_can_only_tighten_the_contract_ceiling`; `test_b3d_a_dimension_the_capability_does_not_expand_must_be_singular` |
| B4 | `test_b4_factor_variation_changes_identity` |
| B5 | `test_b5_entry_variation_changes_identity` |
| B6 | `test_b6_exit_variation_changes_identity` |
| B7 | `test_b7_inherited_parent_semantics_are_materialized_in_the_candidate`; `test_b7b_inherited_factor_and_exit_come_from_the_frozen_pin`; `test_b7c_absent_and_inherit_are_not_the_same_declaration` |
| B8 | `test_b8_arbitrary_executable_payload_is_rejected`; `test_b8b_dynamic_field_lookup_and_unknown_ops_are_rejected`; `test_b8c_structural_mutation_cannot_smuggle_a_second_parameter_authority`; `test_b8d_generator_never_rewrites_parent_structure_implicitly` |
| B9 | `test_b9_same_semantics_across_generators_dedup_to_one_candidate`; `test_b9b_provenance_variation_does_not_split_candidate_identity` |
| B10 | `test_b10_batch_binds_the_frozen_generation_input`; `test_b10b_generation_input_fingerprint_is_content_bound`; `test_b10c_batch_rows_are_append_only_and_have_no_latest_pointer`; `test_b10d_read_model_publishes_evidence_not_an_implicit_latest` |
| B11 | `test_b11_batch_identity_is_not_candidate_identity` |
| B12 | `test_b12_generator_path_has_no_evaluation_promotion_or_execution_dependency`; `test_b12b_no_scoring_ranking_or_winner_selection_in_the_generator_path`; `test_b12c_search_space_module_is_a_pure_contract`; `test_b12d_no_runtime_create_or_alter_table_in_the_generator_path`; `test_b12e_no_implicit_current_state_lookup`; `test_b12e2_repository_clock_is_confined_to_persistence_metadata`; `test_b12f_generator_dispatch_is_a_registry_not_a_branching_chain` |

额外：`CandidateContractUpgradeTests` 证明 v1 行仍自证且 `candidate_json` 逐字不变、
v34 重建幂等且不回填历史 provenance、在**已有 proposal 行且 FK 开启**的 v33 账本上
FK-safe、v2 载荷与**对象**都拒绝携带 provenance、v2 候选表无任何 provenance 列、
早期 v34 的中间形态也会被重建。

## 18. Focused tests

```text
backend/test_r35b_candidate_expansion.py        41 tests  OK   (B1–B12)
backend/test_r35a_strategy_candidate.py         51 tests  OK   (C1–C10, v2 契约同步)
backend/test_strategy_api_contract.py           19 tests  OK   (+ R35-B HTTP journey)
backend/test_db_migrate / test_paper_schema_migrations / test_strategy_dsl
  / test_strategy_parameter_schema / test_strategy_registry
  / test_repository_hygiene / test_frontend_module_contract
  / test_evolution_candidate_activation / test_strategy_version_immutability
                                              228+ tests OK (skipped=1, focused set)
frontend/tests/strategy-candidates.test.mjs     11 tests  pass (R35A-C1…C6 + R35B-C1…C5)
ruff check backend / compileall -q backend / git diff --check   all clean
```

## 19. Mutation result

`work/r35b_candidate_expansion_mutation_check.py`（ID 前缀 **M-X** = eXpansion，刻意
避开 R34-B allocation 已有的 M-B1…M-B20 命名空间）:

```text
M-X1 DETECTED (an oversized or partially-declared space is silently truncated)
M-X2 DETECTED (the canonical fingerprint ignores the factor slot)
M-X3 DETECTED (the canonical fingerprint ignores the exit slot)
M-X4 DETECTED (an inherited slot no longer resolves to the pinned parent's semantics)
M-X5 DETECTED (different generators produce two candidate ids for one specification)
M-X6 DETECTED (the generation input fingerprint stops binding the frozen input)
M-X7 DETECTED (an arbitrary AST is accepted as a slot alternative)
M-X8 DETECTED (the candidate table rebuild is no longer foreign-key safe)
M-X9 DETECTED (provenance is popped from the fingerprint but kept in the payload / object / DB columns)
M-X10 DETECTED (the read model projects the proposal history as an implicit latest)
M-X detected = 10/10
survived = 0; fake = 0; timeout = 0
restore SHA256 = PASS
baseline after restore = GREEN
```

R35-A 的 mutation 在 v2 契约下复验（锚点已随 schema 版本化更新）：

```text
M-G1 … M-G8 DETECTED
M-G detected = 8/8
survived = 0; fake = 0; timeout = 0
restore SHA256 = PASS
baseline after restore = GREEN
```

## 20. Full verification

```text
backend full suite (local, Python 3.14.5)      Ran 5393 tests   OK (skipped=5)
backend full suite (Docker, --network none)    [exact-head CI]
frontend unit tests (node --test)              168 tests  pass 0 fail
Chromium E2E (npx playwright test)             [exact-head CI]
ruff check backend                             All checks passed!
python -m compileall -q backend                clean
git diff --check                               clean
security leak scan                             [exact-head CI]
```

完整 gate 交给 exact-head GitHub CI（tests / docker --network none / frontend /
Chromium E2E / quality / syntax / security）。

## 21. Architecture / maintainability report

```text
Business authority added:
  strategy_candidate_search_space = 候选搜索空间声明契约（显式、有限、可 fingerprint）
  strategy_candidate_service.generation_batch_identity = 生成请求身份 + input fingerprint
  paper_schema_migrations.ensure_strategy_candidates (v34) = 候选表 v2 + batch 表 DDL

Business authority removed: none

Search-space authority:      strategy_candidate_search_space（纯契约，无 DB/registry/时钟）
Candidate authority:         strategy_candidate（纯契约）
Fingerprint authority:       strategy_candidate（canonicalize + sha256）
Dedup authority:             strategy_candidate fingerprint（唯一）
Parent strategy authority:   strategy_registry（exact immutable version + checksum）
Generation batch authority:  strategy_candidate_service 组装 + strategy_candidate_repository 追加
                             （append-only，无 current/latest 指针）

Duplicate implementation removed: NO (none existed)
New facade/wrapper:   0
Old facade/wrapper removed: 0
Implicit current-state lookup added: 0
Direct candidate DB write sites: before = 1 / after = 1（append_candidate）
Direct proposal DB write sites:  before = 1 / after = 1（record_proposal）
Direct batch DB write sites:     before = 0 / after = 1（record_generation_batch）
Large generator if/elif chain added: NO（显式 capability registry + 数据化 dimensions）
Silent candidate-space truncation: NO（超限 fail closed，B3 + M-B1 证明）
Frontend duplicated business rule: NO
Arbitrary generated Python execution path: NO
Generator -> evaluation dependency: NO
Generator -> lifecycle dependency: NO
Generator -> execution dependency: NO

理解 candidate generation 核心规则需要查看：
before = 2 modules（strategy_candidate + strategy_generator）
after  = 3 modules（+ strategy_candidate_search_space：搜索空间是独立 authority，
         能一句话说明"显式、有限、可 fingerprint 的生成输入声明"）

paper_trading.py: LOC 15780 / defs 294（仅趋势观察，不作为 blocker；本 PR 未改其业务逻辑）
```

## 22. 明确不属于 R35-B

LLM strategy generation、AI research → strategy AST、Bayesian optimisation、
genetic / evolutionary algorithm、candidate scoring、backtest scheduler、PIT
experiment controller、walk-forward search、robustness search、automatic winner
selection、Shadow / Paper / Production-Sim promotion、Learning Store feedback、
closed-loop evolution。对应 R35-C / R36 / R37。

完成后停止，等待人工审核。**不自动合并，不开始 R35-C。**
