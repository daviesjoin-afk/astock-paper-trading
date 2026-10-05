# R35-A：Strategy Candidate Contract + Constrained Generator

状态：**IN REVIEW**（等待人工审核，未合并、未部署）
PR：`feat(strategy): add constrained strategy candidate generator`
分支：`codex/r35a-strategy-candidate-generator`

## 阶段认定

```text
R34-A COMPLETE
R34-B COMPLETE
R34-C COMPLETE（PR #227，exact head 0b2bb46，8/8 checks）
R34   COMPLETE

R35-A IN REVIEW
R35   IN PROGRESS
R36   NOT STARTED
R37   NOT STARTED
```

## 一、R35 的定位

R35 进入 **AI-assisted Strategy Evolution**。本阶段**不是**：

- 让 AI 自动修改正在运行的正式策略 / 覆盖 production strategy
- 让 AI 自动晋级策略 / 修改风险规则 / 调整 validation threshold
- 让 AI 自动进入 Production-Sim
- 让 LLM 任意生成 Python 后直接执行

R35-A 的唯一核心权限是 `produce StrategyCandidate`。链路必须保持：

```text
Generator → StrategyCandidate → Experiment / Validation
          → Lifecycle / Promotion → Shadow / Paper / Production-Sim
```

严禁 `Generator → Execution`，也严禁 `Generator → 直接覆盖 active strategy`。

## 二、四个 capability 与它们的 authority

| 模块 | 业务职责 | 负责的事实 | 依赖 |
| --- | --- | --- | --- |
| `strategy_candidate.py` | candidate 是什么（纯契约） | canonical identity / fingerprint / 输入校验 | stdlib + `strategy_dsl_schema` + `strategy_parameter_schema` |
| `strategy_generator.py` | 从显式输入生成候选（纯边界） | generator 语义、受约束变体展开 | stdlib + `strategy_candidate` + 既有 DSL/参数契约 |
| `strategy_candidate_repository.py` | 候选台账怎么存（append-only） | candidate 行 + 提案证据 | stdlib + `strategy_candidate` |
| `strategy_candidate_service.py` | exact pin + 编排 | parent pin 的建立与追加 | registry + 上述三者 |

调用深度：API → service → (generator → candidate) + repository。没有 helper / utils /
common / manager / facade 包装层。

## 三、StrategyCandidate 表达的事实

`candidate_id`（= canonical fingerprint）、`parent_strategy_id` /
`parent_strategy_version` / `parent_strategy_checksum`、`generator_type` /
`generator_version` / `generator_contract_version`、`hypothesis_id` /
`research_provenance`、`strategy_schema_version`、`factor_spec` / `entry_spec` /
`exit_spec` / `parameter_spec`、`universe_spec`、`intended_market_regime`、
`constraints`、`asof`、`random_seed`、`model_identity`、`candidate_schema_version`。

**明确区分** candidate identity 与 candidate evaluation result。台账与契约里没有
Sharpe / 收益率 / 回撤 / 胜率 / promotion 结果；`FORBIDDEN_EVALUATION_KEYS` 出现即
fail closed。

## 四、immutable 与 canonical fingerprint

- 已记录的 candidate 不得原地修改：`CHECK(candidate_id = candidate_fingerprint)`
  + `no_update` / `no_delete` trigger。改动语义事实产生**新** candidate。
- fingerprint = 全部语义事实，减去 `candidate_id` / `candidate_fingerprint` 与持久化
  元数据 `created_at`。canonicalize（`sort_keys` + 紧凑分隔符）后再 hash，禁止
  `hash(str(dict))`。
- 会改变 fingerprint：parent version / checksum、factor / entry / exit 规则、参数值、
  DSL schema version、generator 语义版本、constraints、universe、intended regime。
- 不改变 fingerprint：数据库 row id、显示名称、UI 排序、无业务意义的 JSON key 顺序。

## 五、受约束表示与 generator 边界

复用既有 owner，不发明新语言：

- 表达式：`strategy_dsl_schema`（封闭 op 集；没有 `python` / `eval` / `exec` /
  `import` / 属性访问，因此任意可执行 payload 是**结构上不存在**，不是子串过滤）。
- 参数：`strategy_parameter_schema`（allowlist / bounds / `max_step` / locked /
  `min_evidence`）。
- `GeneratorInput` 显式携带全部事实；generator 内部无 DB、无 registry、无
  `datetime.now()` 业务 as-of、无 current/latest 读取。缺任一必需事实即 fail closed，
  绝不偷偷用 latest 补齐。

## 六、Dedup authority

唯一权威是 canonical candidate fingerprint。同一个候选再次被提出时身份不变、行数不增；
每次提案的来源证据追加进 `strategy_candidate_proposals`。禁止以名字相同 / 描述相似 /
LLM 文本相似 / 创建时间相近作为去重依据。

## 七、Persistence

migration **v33**（`paper_schema_migrations.ensure_strategy_candidates`）是 DDL 唯一
owner；业务代码运行时不做 `CREATE TABLE` / `ALTER TABLE`。两张 append-only 表：
`strategy_candidates`（15 列，无评估列）与 `strategy_candidate_proposals`。**不回填**：
升级前不存在"候选"这个事实。

## 八、测试与变异

- C1–C10 contract 回归：`backend/test_r35a_strategy_candidate.py`（51 tests）。
- HTTP 契约：`backend/test_strategy_api_contract.py` 的
  `test_r35a_candidate_generation_and_read_model_over_http`。
- 前端只读事实：`frontend/tests/strategy-candidates.test.mjs`（6 tests）。
- 变异：`work/r35a_candidate_mutation_check.py`（M-G1…M-G8，8/8 DETECTED）。

### 人工复核 P2 修复

- 父策略 `metadata.constraints` 在调用方未给 override 时必须被**继承**（空集不是
  "无约束"，而是丢掉父策略的仓位/敞口/权重上限）。
- 提案身份必须是**独立的事件身份**：`proposal_id` 是 opaque event id
  （`secrets.token_hex(32)`），不依赖进程内计数器 / PID / thread id / 墙上时钟 /
  内容哈希作为唯一性权威。同一秒内同一输入的两次提案是两条独立历史记录，事件表写入
  为 fail-closed 的 `INSERT`（碰撞报错），不能被 `INSERT OR IGNORE` 静默合并。

## 九、明确不属于 R35-A

Bayesian optimisation、evolutionary population search、大规模参数搜索、自动
walk-forward、自动 PIT backtest queue、自动 robustness queue、AI 自动修改生产策略、
自动 Shadow / Paper / Production-Sim promotion、Learning Store feedback loop。
这些属于 R35-B/C、R36、R37，不提前混入。
