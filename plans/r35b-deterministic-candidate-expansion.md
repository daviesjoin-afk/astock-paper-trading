# R35-B — Deterministic Candidate Expansion

阶段：**R35 IN PROGRESS**（R35-A COMPLETE、R35-B IN REVIEW、R35-C NOT STARTED）

## 一、定位

R35-A 回答"StrategyCandidate 是什么"（canonical identity / fingerprint / exact parent
pin / candidate ledger / proposal event ledger）。R35-B 回答"系统能生成哪些**受约束
策略候选**"，仍然**不**回答"哪个候选赚钱 / 哪个最好 / 哪个应该晋级"。

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

R35-B 明确**不是** Strategy Evaluation、不是 Experiment Search、不是 AI autonomous
optimization：不 backtest、不 PIT validation、不 robustness、不 scoring、不 ranking、
不选 winner、不 promotion、不 lifecycle transition、不 execution、不 allocation、
不 risk override。这些属于 R36 / R37。

## 二、新增的 capability

| 模块 | 业务职责（一句话） | 依赖谁 |
| --- | --- | --- |
| `strategy_candidate_search_space.py` | **搜索空间是什么**：显式、有限、可 fingerprint 的生成输入声明 + 组合基数 + slot 继承语义 | stdlib + `strategy_candidate` + `strategy_dsl_schema` |
| `strategy_generator.py`（扩展） | **确定性候选展开**：把一份已冻结的 search space 展开成 `StrategyCandidate` 元组 | stdlib + 上述 + `strategy_parameter_schema` |
| `strategy_candidate.py`（v2） | candidate 内容身份（generator 能力身份移出指纹） | 同 R35-A |
| `strategy_candidate_repository.py`（扩展） | candidate / proposal / **generation batch** 三条 append-only 台账 | stdlib + `strategy_candidate` |
| `strategy_candidate_service.py`（扩展） | exact pin + search space 组装 + batch 身份 + 追加 | registry + 上述三者 |
| `paper_schema_migrations.py`（v34） | DDL 唯一 owner：候选表 v2 重建 + batch 追加表 | — |

调用深度：API → service → (search space → generator → candidate) + repository。

## 三、三种身份，三套契约

```text
candidate row  → content identity (canonical fingerprint)      → semantic dedup
proposal row   → opaque event identity (secrets.token_hex(32)) → append every occurrence
batch row      → opaque request identity                       → append every request
```

generator **能力身份**（type / version / contract version）是 proposal 事件与 batch 的
provenance，**不**是 candidate fingerprint 的一部分。因此同一份 canonical
specification 由 `factor_variant` 与 `bounded_combination` 分别提出时：候选行 1 条、
proposal 事件 2 条。这一条与 R35-A 的"candidate vs proposal"是同一个原则的延续。

candidate schema 因此升级为 `strategy-candidate-v2`：

- 候选表去掉三个 `NOT NULL` 的 generator 列（留着会逼每个新行编一个能力身份，
  那正是要消除的第二套 identity authority）；
- migration **v34** 重建候选表：forward-only、幂等、**不回填**；历史 v1 行的
  `candidate_json` 逐字保留，仍按 v1 材料自证（`LEGACY_GENERATOR_IDENTITY_KEYS`）；
- 新增 `strategy_candidate_generation_batches`（append-only，无 `current/latest` 指针）。

## 四、Generator 能力（显式 registry，不是 if/elif 链）

| generator_type | 展开的维度 | 版本 |
| --- | --- | --- |
| `parameter_variant` | parameters | v1 |
| `factor_variant` | factor | v1 |
| `entry_variant` | entry | v1 |
| `exit_variant` | exit | v1 |
| `bounded_combination` | parameters × factor × entry × exit | v1 |

`GeneratorCapability.dimensions` 是**唯一**的能力语义声明；展开本身是同一个确定性
笛卡尔展开。新增能力不需要碰展开逻辑，也不可能出现"某个能力偷偷多展开一个维度"。

## 五、边界（写进契约与测试）

- **基数先算，超限拒绝**：`cardinality` 在生成前可算；超过 128 一律 fail closed，
  **绝不静默截断**（截断让 candidate universe 依赖遍历顺序）。
- **声明顺序不承载语义**：JSON key 顺序 / 参数声明顺序 / alternative 顺序都不改变
  candidate 集合；输出按 `candidate_id` canonical 排序。
- **slot 语义唯一**：`inherit_parent` / `explicit_variant` / `absent` 互斥；不允许用
  `None` 同时表示三件事。`entry` 不能是 `absent`（候选契约要求 entry_spec 必需）。
- **继承在生成阶段落地**：`inherit_parent` 解析成 exact pinned parent 那一版的最终
  值并写进 candidate，绝不留下"未来回读 registry"的悬空引用。
- **constraints：inherit, or only tighten**。放宽仓位 / 敞口 / 权重上限是风险放大
  动作，必须走正式 risk evidence gate，不属于 candidate generator。
- **不做自由 AST mutation**：只能替换调用方在明确 slot 里给出的、已经过
  `strategy_dsl_schema` 校验的备选；不得随机增删节点 / 反转比较符 / 替换 field。
- **universe 只是声明**：候选只保存 universe declaration / identity；某历史日期能否
  交易仍由 Market Data / Tradability authority 判定，generator 不复制 PIT 规则。
- **不写策略研究知识**：没有 `if market_regime == "bull"` 这类分支。

## 六、测试与变异

- B1–B12 contract 回归：`backend/test_r35b_candidate_expansion.py`（33 tests）。
- HTTP 契约：`backend/test_strategy_api_contract.py` 的
  `test_r35b_search_space_generation_over_http`。
- 前端只读事实：`frontend/tests/strategy-candidates.test.mjs`（R35B-C1…C3）。
- R35-A 回归按 v2 契约同步更新：`backend/test_r35a_strategy_candidate.py`（51 tests）。
- 变异：`work/r35b_candidate_expansion_mutation_check.py`（M-B1…M-B8，8/8 DETECTED）；
  R35-A 的 `work/r35a_candidate_mutation_check.py` 保持 M-G1…M-G8 8/8 DETECTED。

## 六之二、v34 重建必须 FK-safe

`strategy_candidate_proposals.candidate_id` 引用候选表，生产连接开着
`PRAGMA foreign_keys=ON`。直接 DROP 被引用的父表会让升级一个已有 proposal 行的 v33
账本报 `FOREIGN KEY constraint failed`（初始化直接失败）；事务内
`PRAGMA foreign_keys=OFF` 是 no-op。因此重建走引用重写：staged 父表 → 子表改指向
staged 父表 → DROP 旧父表 → staged 父表 RENAME 回真名。外键重建后仍然强制，由
`CandidateContractUpgradeTests` 与 M-B8 覆盖。

## 七、明确不属于 R35-B

LLM strategy generation、AI research → strategy AST、Bayesian optimisation、
genetic / evolutionary algorithm、candidate scoring、backtest scheduler、PIT
experiment controller、walk-forward search、robustness search、automatic winner
selection、Shadow / Paper / Production-Sim promotion、Learning Store feedback、
closed-loop evolution。

对应后续：R35-C（AI hypothesis → constrained candidate generation）、
R36（Experiment Search Controller）、R37（Closed-loop Learning / autonomous
Strategy Factory）。
