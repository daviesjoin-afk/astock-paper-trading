# R35-C — AI Hypothesis → Constrained Candidate Generation

```text
R34     COMPLETE
R35-A   COMPLETE
R35-B   COMPLETE
R35-C   IN REVIEW
R35     IN PROGRESS
R36     NOT STARTED
R37     NOT STARTED

MERGE: NOT DONE
DEPLOY: NOT DONE
R36: NOT STARTED
```

```text
exact base SHA:    f6f1b98622af87b24bd8256cac951e723397689f
                   （PR #229 合并后的 master）
feature commit:    见 GitHub 的 commit 列表
exact head SHA:    见 GitHub 的 headRefOid（其后的提交仅改文档，不改代码）
branch:            codex/r35c-ai-hypothesis-candidate-generation
```

## 0. 这一轮回答什么

```text
R35-A  StrategyCandidate 是什么？
R35-B  一个明确、有限的 search space 如何确定性展开成 StrategyCandidate？
R35-C  一个已经存在、可审计的 AI research hypothesis
       如何提出一个**受约束**的 candidate search space？
```

完整链路：

```text
R27 canonical Research Run
        ↓ exact research_run_id（无 latest fallback）
strict research gate（authority=research / is_authoritative=false / status=supported）
        ↓
AI Candidate Proposal（bounded search-space declaration）
        ↓ strict bounded proposal contract
R35-B CandidateSearchSpace
        ↓ R35-B deterministic generator
StrategyCandidate[]
        ↓ candidate / proposal / batch ledgers（既有三张表，零新表）
```

## 1. 新增模块（3 个，各有一句话职责）

| 模块 | authority |
|---|---|
| `backend/strategy_ai_proposal.py` | **AI 可以提出什么** —— proposal schema（形状 / 资源上界 / 禁止字段 / no-op） |
| `backend/strategy_ai_provider.py` | **如何得到严格 proposal** —— prompt → transport → 严格解析 |
| `backend/strategy_ai_candidate_service.py` | **exact research → proposal → R35-B batch** 的编排与失败语义 |

理想拆分而非机械拆文件；没有新增 `ai_strategy_manager` / `ai_strategy_facade` /
`ai_candidate_utils` / `candidate_generation_helper` / `research_candidate_common` /
`proposal_utils` / `ai_candidate_helper` / `candidate_ai_facade`。

## 2. 复用 R27，禁止第二套

复用 `ai_research_contract`（typed InformationEvent / ResearchHypothesis / PIT boundary）、
`ai_research_repository`（canonical append-only ledger / `get_run` / `record_hash`）、
`ai_provider_transport`（唯一网络出口）、`ai_review_service`（既有 provider 槽位配置）。

禁止第二套 hypothesis / research DB / provider transport / research status / evidence
verification / DSL 校验 / parameter 校验 / provider 配置系统。R35-C 生产模块中不存在
`requests.post` / `urllib.request.urlopen` / `http.client` / `OpenAI(` / `Anthropic(`。

**R27 的 consumer allowlist 是有意识的决定**：本轮显式登记
`strategy_ai_candidate_service.py` 到
`test_ai_research_contract.ALLOWED_AI_CONSUMERS` 与
`test_ai_provider_transport.test_RG_05`。

## 3. AI 研究不是交易 authority

`ResearchHypothesis.authority == "research"`、`is_authoritative == False` 保持不变。
即使 `status == supported` 且 `confidence == 1.0`，也只意味着"允许产生**研究候选**"，
绝不意味着 signal approved / strategy validated / promotable / risk approved / order
allowed。

资格 gate **只看 `status == supported`**，不设任何 confidence 阈值 —— R27 已明确
confidence 是 AI 自评而非证据，把它请回来就是把"AI 越自信结论越强"这条环路重新装上。
C3 用 confidence 0.1 与 0.9 两条 run 证明资格逻辑完全一致。

## 4. 只接受 exact research run

请求必须带 `research_run_id`；读取只经 `ai_research_repository.get_run`。**不存在**
`recent_runs(...)[0]` / latest research / current hypothesis 作为隐式输入。run id 只接受
规范十进制整数：`" 7"` / `"7.0"` / `"latest"` / `"*"` / `""` / 负数一律 fail closed。
C1/C1b 覆盖，且 C1c 用 AST 断言服务层不**调用**任何"取最近研究"的读取。

## 5. 拒绝排在付费调用之前

`plan_research_candidate_generation()` 是读的一半，存在的唯一理由是把拒绝排在网络调用
之前：run not found / authority 不符 / unsupported / corrupt / as-of 不一致全部在
provider 之前失败。C2 断言 unsupported research 的 `provider call count = 0` 且
`candidate writes = 0`。

顺序不是风格问题：反过来的话一次坏输入会先花掉一次 provider 调用。

## 6. 业务日期钉死

```text
candidate generation asof  ==  research run as_of
```

不允许拿 2026-09-01 的 hypothesis 自动生成 2026-10-06 的候选。省略 `asof` 即采纳
**research run 自己的业务日**（不是"用今天"）；显式给值必须完全一致。调用方（尤其前端）
无从知道那条 run 的 as_of，让它自己填就等于让它制造业务日事实。C4/C4b/C4c 覆盖。

## 7. AI 输出不是 StrategyCandidate

provider **不允许**输出 `candidate_id` / `candidate_fingerprint` /
`candidate_schema_version` / parent checksum / `batch_id` / `proposal_id` / `status` /
`score` / `rank` / `promotion`。AI 只输出一个 bounded candidate proposal，然后经 R35-B
变成 StrategyCandidate。Candidate identity 仍然只有 `strategy_candidate` 一个 authority。

## 8. Provider 严格 schema

顶层只允许 `parameter_variants` + `factor_slot` / `entry_slot` / `exit_slot`。出现任何
禁止字段即 fail closed，**不是**静默忽略：parent 三件套、candidate identity、asof、
universe / regime / constraints、generator 三件套、`max_candidates` / `evidence_count`、
`hypothesis_id` / `research_provenance` / `model_identity`，以及
status / score / rank / sharpe / return / drawdown / winner / promotion / deploy /
execution / order / position / risk_override。

**越权尝试与协议漂移必须可区分**：前者 `invalid_proposal_field`，后者
`unknown_proposal_field`。C7c 逐个遍历 `FORBIDDEN_PROVIDER_FIELDS` 要求给出前者 ——
这条断言让该集合成为**承载语义**的边界（M-AIG5 曾 SURVIVED 正是因为两者给出了同一个
reason，见 §16）。

## 9. AI 不拥有 constraints / universe / regime

R35-B 已确立 constraints = inherit 或 only tighten；R35-C 再进一步 —— AI provider
**根本不拥有** `constraints` 字段。AI 路径默认 `constraints = exact pinned parent
constraints`。R35-C 不开放 AI risk tuning；调整 risk limits / max exposure / max weight /
max positions 必须经过独立 risk authority。同理 universe / regime 由 caller 显式提供或
从 exact parent 继承。

## 10. Generator 固定为 R35-B authority

AI 不选择 generator：AI 路径固定 `generator_type = bounded_combination`（R35-B registry
当前明确支持的版本）。AI 只提出有限备选，"如何展开"是 R35-B 的职责；搜索控制器属于 R36。

## 11. AI-specific cap 与资源上界

```text
MAX_AI_CANDIDATES_PER_REQUEST = 32      （≤ R35-B 全局上限 128）
MAX_AI_PARAMETERS             = 8
MAX_AI_VALUES_PER_PARAMETER   = 8
MAX_AI_ALTERNATIVES_PER_SLOT  = 8
MAX_PROVIDER_TOKENS           = 1200
```

调用方只能继续收紧（16 OK；33/64/128 reject）。超限 fail closed，**绝不截断**。资源上界
在**任何** AST / 字段校验之前生效，避免"10000 个 alternative 最后去重只剩 3 个"先把
解析器撑爆。C10 用 5 个合法参数值证明"超限拒绝"与"静默截断"可区分。

## 12. DSL / parameter 合法性的既有 owner 不变

provider 返回的 AST 一律经 `strategy_dsl_schema` / `strategy_candidate_search_space`；
parameter 一律经 `strategy_parameter_schema`。没有 `validate_ai_ast`，没有第二套
op/field/operator allowlist，没有第二套 parameter validator（C9b 用源码断言钉住）。
禁止自由源码（python / eval / exec / shell / SQL / source_code / lambda）进入候选。

## 13. 禁止"自动修复"

unknown op / invalid parameter / risk-expanding proposal / oversized search space
一律 **reject**：不自动删节点、不 clamp、不截断、不换成 parent 值、不把 invalid operator
换成相近 operator —— 那会让实际候选不再等于 AI 提出的候选。

## 14. no-op proposal 拒绝

全部 inherit、无任何参数或角色变体 = 什么都没提出；生成它等于把父策略自己当成 AI 候选。
直接 `no_op_proposal`。`absent` **算**已声明的变化（它显式声明"本候选没有该角色"），
空备选的 `explicit_variant` 不算。

## 15. Provenance 与 model identity

```text
research_provenance.source_kind        = ai_research
research_provenance.source_identity    = ai_research_run:<exact id>
research_provenance.source_fingerprint = exact ai_research_runs.record_hash
research_provenance.hypothesis_id      = exact hypothesis_id
```

`source_identity` 绝不是 `"latest-ai-research"`；`source_fingerprint` 直接用 R27 已有的
`record_hash`（R27 是 integrity authority），不重新 hash 一份替代品。

`model_identity` 表示"**哪个 model 把 hypothesis 转成 candidate proposal**"，与"research
run 最初由哪个 model 产生 hypothesis"是两个概念，不混用；provider 没给可靠 version 就
留空、不编造。

`evidence_count` 从 exact research run 的 canonical 投影派生，AI 无权自述。

## 16. 网络绝不在 SQLite 写事务里 + 失败语义

```text
1. 短读    exact research run + exact parent pin facts
2. 关闭读连接
3. AI provider 网络调用            ← 绝不持有写锁等 LLM
4. 严格解析 + R35-B 校验
5. 短写    重新 pin exact parent → R35-B 生成 + 追加
6. commit
```

绝不出现 `BEGIN IMMEDIATE → 等 LLM 30 秒 → INSERT`。C14 在 provider 调用**期间**从另一条
连接取写锁，取得到才说明调用方没有持有它。

* **provider 失败零写入**：batch / proposal / candidate delta 全为 0；已有旧 candidate
  也不会被算成本次成功结果（C15 / C15b）。
* **写事务 all-or-nothing**：第 N 个 proposal 写失败 → 整次 batch rollback（C16）。

## 17. 零新 DB authority + batch 可完整审计

不建第四套候选台账（`strategy_ai_candidates` / `ai_generated_strategies` /
`llm_candidate_table` / `strategy_ai_proposals_db` / `strategy_ai_proposal_runs`），产物
继续写既有三张表。持久审计链：

```text
ai_research_runs（exact run + record_hash）
        ↓ research provenance
generation batch（canonical search space + model identity + input fingerprint）
        ↓ proposal events → candidate rows
```

`batch_json` 现在持久化 canonical search-space material + 调用方材料 + fingerprint，并能
重算校验 `hash(canonical(material)) == search_space_fingerprint`
（`SCV.verify_batch_search_space_material`，C17）。历史 R35-A/B batch 没有完整 material：
**保持 legacy、不回填**，也不凭 candidate 反推旧搜索空间（C17c）。

## 18. API 与前端

```text
POST /api/strategies/{strategy_id}/candidate-generations/ai
```

请求刻意更窄：`strategy_version` / `strategy_checksum` / `research_run_id` / 可选
`asof` / 可选 `universe_spec` / `intended_market_regime` / `provider_slot` /
`max_candidates ≤ 32`。**没有** generator / constraints / provenance / evidence_count，
客户端也无法提交 `api_key` / `base_url` / `Authorization` —— provider 配置复用既有
`ai_review_service` 槽位 authority，不新增 `STRATEGY_AI_API_KEY` 第二套配置。

route 只做 typed 请求、provider 配置解析、调用 service、错误映射；不做 research gate /
parent 查找 / prompt 组装 / AST 校验 / 候选展开 / DB INSERT。拒绝映射：不存在 404、
research 不自洽 409、请求越界与 search space 被拒 400、provider 协议/transport 失败 502，
**不**返回 5xx。

前端 `wbAiCandidateHtml()` 只做最小能力：输入 exact research run、显式点击生成、渲染
`research_run_id` / `hypothesis_id` / research record hash / provider model / parent pin /
search-space fingerprint / generation batch id / candidate count / candidate ids。
**禁止** AI 推荐策略 / 最佳策略 / 最优参数 / 预计收益 / AI Score / 一键上线 / Promote /
Deploy；不自动触发（无 scheduler / cron）；不替后端制造 as-of / universe / regime。
未发起生成时明说"尚未发起生成"，不编造结果。

inline handler 必须挂到 `window`（`frontend/src/bridge.js` 的 `window.wbAiGenerateCandidates`）：
完整套件里的 `test_frontend_module_contract.BridgeCoverageTests` 抓到过一次漏挂 ——
那种按钮在浏览器里会**静默失效**，而单看源码完全看不出来。

## 19. 测试

```text
backend/test_r35c_ai_candidate_generation.py    C1–C18 契约（53 tests）
backend/test_r35c_ai_candidate_http.py          HTTP 契约（8 tests）
                                                R35-C 合计 61 tests
backend full suite (local, Python 3.14.5)       Ran 5454 tests  OK (skipped=5)
frontend (node --test)                          173 tests  pass 0 fail
ruff check backend / compileall -q backend / git diff --check   all clean
```
backend/test_r35a_strategy_candidate.py         R35-A 回归
backend/test_r35b_candidate_expansion.py        R35-B 回归
backend/test_ai_research_contract.py            R27 契约与 consumer allowlist
backend/test_ai_research_repository.py          R27 canonical ledger
backend/test_ai_research_service.py             R27 orchestration
backend/test_ai_provider_transport.py           唯一网络出口 + RG_05 allowlist
backend/test_strategy_dsl.py                    DSL authority
backend/test_strategy_parameter_schema.py       parameter authority
backend/test_strategy_registry.py               精确 parent 版本
backend/test_strategy_api_contract.py           API 契约
backend/test_db_migrate / test_paper_schema_migrations / test_repository_hygiene
frontend/tests/strategy-candidates.test.mjs     R35A-C1…C6 + R35B-C1…C5 + R35C-C1…C5
```

### Mutation

```text
M-AIG1  DETECTED (exact research run id falls back to the recent run)
M-AIG2  DETECTED (the research support gate is removed)
M-AIG3  DETECTED (confidence is promoted into an eligibility threshold)
M-AIG4  DETECTED (the candidate asof stops being pinned to the research run)
M-AIG5  DETECTED (the provider may declare risk, universe, regime or asof)
M-AIG5b DETECTED (the provider may choose the parent strategy)
M-AIG6  DETECTED (unknown provider fields are silently ignored)
M-AIG7  DETECTED (an over-cap proposal is truncated instead of rejected)
M-AIG8  DETECTED (the generation provenance stops binding the exact research record hash)
M-AIG9  DETECTED (a no-op proposal is accepted)
M-AIG10 DETECTED (the proposal model identity is replaced by the research run model)
M-AIG detected = 11/11
survived = 0; fake = 0; timeout = 0
restore SHA256 = PASS
baseline after restore = GREEN
```

**M-AIG5/M-AIG5b 曾经 SURVIVED，发现一个真实缺陷**：`FORBIDDEN_PROVIDER_FIELDS` 当时是
**装饰性**的 —— 其中每个字段同时也不在允许集合里，于是"AI 试图声明越权字段"与"协议
漂移"落到同一个 `unknown_proposal_field`，越权事件在审计里消失。现在两者给出不同
reason，并由 C7c 逐个遍历禁止集合钉住。

R35-A / R35-B mutation 在本轮契约下复验：

```text
M-G1 … M-G8    8/8  DETECTED
M-X1 … M-X10  10/10 DETECTED
survived = 0; fake = 0; timeout = 0; restore SHA256 = PASS; baseline GREEN
```

（M-X6 的锚点随本轮 batch material 增强而更新；R35-B 的 `generation_input_fingerprint`
现在同时绑定 canonical search-space material。）

## 20. 架构 / authority 报告

```text
Business authority added:
  AI candidate proposal schema（strategy_ai_proposal）
  AI provider response → strict proposal（strategy_ai_provider）
  exact research → AI proposal → R35-B batch 的编排与失败语义（strategy_ai_candidate_service）

Business authority removed:
  无（本轮只新增受约束的上游入口）

Research hypothesis authority:      ai_research_contract（不变，仍是 advisory / research only）
AI proposal authority:              strategy_ai_proposal
Search-space authority:             strategy_candidate_search_space
Candidate identity authority:       strategy_candidate
Parent authority:                   strategy_registry（exact version，AI 无权选择）
Provider transport authority:       ai_provider_transport（唯一网络出口，不变）
Generation batch authority:         strategy_candidate_repository / service

New facade/wrapper: 0

Duplicate implementation removed: NO（本轮未移除既有重复实现）

Implicit latest/current research lookup added: 0
Implicit latest/current strategy lookup added: 0

Direct candidate DB write sites:        before 1 / after 1（仍只在 strategy_candidate_repository）
Direct proposal DB write sites:         before 1 / after 1
Direct generation batch DB write sites: before 1 / after 1

New AI-specific DB tables: 0

Direct provider HTTP call sites outside ai_provider_transport: 0

Generator -> evaluation dependency: NO
Generator -> promotion dependency:   NO
Generator -> execution dependency:   NO

AI -> risk relaxation path:            NO（AI 根本不拥有 constraints 字段）
AI -> formal strategy mutation path:   NO（产物只能是 StrategyCandidate）

Frontend duplicated business rule: NO

Large provider/generator if/elif chain: NO（generator 仍是 R35-B 的 registry）

理解 AI candidate generation 核心规则需要查看：
  before = 0 modules（能力不存在）
  after  = 3 modules（proposal / provider / service）

paper_trading.py: LOC / defs —— 仅趋势观察，不作为 blocker
```

## 21. 可维护性报告

```text
新增模块数：3
新增 facade / helper：0
第二套 research / provider / transport / DSL / parameter 校验：0
第二套 provider 配置系统：0
R27 consumer allowlist 变更：+1（有意识登记，非自动放行）
```

## 22. 明确不属于 R35-C（R36 / R37）

candidate backtest、Sharpe ranking、best candidate、walk-forward search controller、
Bayesian optimisation、evolutionary / genetic search、automatic parameter search loop、
automatic retry based on performance、automatic candidate elimination、PIT scheduler、
robustness scheduler、winner selection、shadow / paper / production-sim activation、
promotion、retirement、Learning Store feedback、closed-loop regeneration。

## 23. non-blocking follow-up（不在本轮）

`candidate_from_projection` 最终应只接受明确支持的 v1 / v2 schema、未知 schema
fail closed。R35-C 完全不需要修改 candidate deserialization，因此不顺手扩大本 PR，
记录为 follow-up。
