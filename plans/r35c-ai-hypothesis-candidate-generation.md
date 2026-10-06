# R35-C — AI Hypothesis → Constrained Candidate Generation

## 一、这一轮回答什么

```text
R35-A：StrategyCandidate 是什么？
R35-B：一个明确、有限的 search space 如何确定性展开成 StrategyCandidate？
R35-C：一个已经存在、可审计的 AI research hypothesis
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

**AI 只负责提出 hypothesis-driven search space。** AI 不负责 candidate identity、
fingerprint、evaluation、ranking、winner selection、promotion、risk relaxation、
execution、portfolio allocation。

## 二、新增模块（3 个，各有一句话职责）

| 模块 | authority |
|---|---|
| `strategy_ai_proposal.py` | **AI 可以提出什么** —— AI candidate proposal 的 schema（形状 / 资源上界 / 禁止字段 / no-op），纯契约，无 DB / 网络 / registry / 时钟 |
| `strategy_ai_provider.py` | **如何得到严格 proposal** —— prompt 组装 → `ai_provider_transport.call_json` → 严格解析 |
| `strategy_ai_candidate_service.py` | **exact research → proposal → R35-B batch** 的编排与失败语义 |

`strategy_ai_proposal` 是纯契约：只验证 JSON shape、资源上界与禁止字段。DSL 与
parameter 合法性一律委托既有 owner（`strategy_dsl_schema` /
`strategy_candidate_search_space`）。绝不复制 `validate_ai_ast` 或第二套
op/field/operator allowlist。

不新增 facade / helper（无 `ai_strategy_manager` / `ai_candidate_utils` /
`proposal_utils` / `candidate_ai_facade`）。

## 三、复用 R27，禁止第二套

复用：

```text
ai_research_contract      typed InformationEvent / ResearchHypothesis / PIT boundary
ai_research_repository    canonical append-only ledger / get_run / record_hash
ai_provider_transport     唯一网络出口
ai_review_service         既有 provider 槽位配置 authority
```

禁止：第二套 hypothesis、第二套 research DB、第二套 provider transport、第二套
research status、第二套 evidence verification、第二套 DSL/parameter 校验、第二套
provider 配置系统（不新增 `STRATEGY_AI_API_KEY`）。R35-C 生产模块中不存在
`requests.post` / `urllib.request.urlopen` / `OpenAI(` / `Anthropic(`。

**R27 的 consumer allowlist 是有意识的决定**：新增消费者必须显式登记（见
`test_ai_research_contract.ALLOWED_AI_CONSUMERS` 与
`test_ai_provider_transport.test_RG_05`），本轮登记了
`strategy_ai_candidate_service.py`。

## 四、AI 研究不是交易 authority

`ResearchHypothesis.authority == "research"`、`is_authoritative == False` 保持不变。
即使 `status == supported` 且 `confidence == 1.0`，也只意味着"允许产生**研究候选**"，
绝不意味着 signal approved / strategy validated / promotable / risk approved / order
allowed。

资格 gate 只看 `status == supported`。**不设任何 confidence 阈值** —— R27 已明确
confidence 是 AI 自评而非证据；把 `confidence >= 0.7` 请回来就是把"AI 越自信结论
越强"这条环路重新装上。C3 用 confidence 0.1 与 0.9 两条 run 证明资格逻辑完全一致。

`supported` 的唯一含义：这条历史 research artifact 有足够已核验证据支持，允许拿来
产生研究候选。它不是 candidate quality，不是 promotion eligibility，不是 expected
return。

## 五、只接受 exact research run

请求必须带 `research_run_id`；读取只经 `ai_research_repository.get_run(conn, id)`。

**不存在** `recent_runs(...)[0]`、`latest research`、`current hypothesis` 作为隐式输入。
run id 只接受**规范十进制整数**：`int > 0`，或匹配 `[1-9][0-9]*` 的字符串。
`"7"` → 7；`" 7"` / `"7 "` / `"007"` / `"+7"` / `"-1"` / `"7.0"` / `"latest"` / `"*"` /
`""` / `bool` 一律 fail closed —— 任何"帮调用方猜一个 run"的行为都是隐式 latest 的入口。

刻意**不做** `strip()` 后再解析：那会让 `" 7"` 悄悄变成 7，于是"形状不合法"被伪装成
"查无此行"，两种完全不同的拒绝在审计上无法区分，回归也会变成**假绿**（只要那个 id 恰好
不存在就"通过"）。形状拒绝因此发生在**读账本之前**。C1b/C1b2/C1b3 覆盖。

不存在该 run → `research_run_not_found`（HTTP 404）。

## 六、拒绝必须排在付费调用之前

`plan_research_candidate_generation()` 是**读**的一半，存在的唯一理由是把拒绝排在网络
调用之前：

```text
run not found / authority != research / is_authoritative
unsupported / insufficient_evidence / corrupt record
as-of 与 research run 不一致
        ↓ 全部在这里拒绝
provider readiness（凭据 / enabled / 地址 / model）
        ↓ 未就绪同样在这里拒绝
        ↓ 之后才允许 provider 调用
```

C2 断言 unsupported research 的 `provider call count = 0` 且 `candidate writes = 0`。
顺序不是风格问题：反过来的话，一次坏输入会先花掉一次 provider 调用。

**provider readiness 必须是本 service 的强制 gate，不能只放 HTTP route**：本模块的
authority 就是"一次 AI 候选生成的完整编排边界"，它可以被 CLI / R36 / scheduler /
其他内部调用**直接**调用；而 `ai_provider_transport.call_json` 刻意只检查
api_key / base_url / model、**不认识** `enabled`。gate 只放 route 等于"绕过 route 时
禁用形同不存在"。规则**复用** `ai_review_service.slot_readiness`（R27-B2B 建立的
canonical 定义），因此禁用槽位在 AI 路径与 R27 research runtime 上判定一致，
不会出现第二套 readiness 语义。route 只负责把 service 的稳定 reason 映射成 HTTP
状态码，不重复判定。`ProviderReadinessTests` + M-AIG11 覆盖。

## 七、业务日期钉死

```text
candidate generation asof  ==  research run as_of
```

不允许拿 2026-09-01 的 hypothesis 自动生成 2026-10-06 的候选。要在新业务日使用同一
思想，必须**重新产生**新的 canonical research run —— 不隐式复用历史研究。C4 覆盖。

## 八、Parent 继续 exact pin

请求继续显式带 `strategy_id` / `strategy_version` / `strategy_checksum`，并复用
`strategy_candidate_service.pin_parent_strategy()`。禁止 `current strategy` /
`latest version` / `MAX(version)` / `active strategy`。**AI 无权选择 parent**：provider
甚至看不到"请选择当前最优策略"，它接收的是已经冻结的 exact parent（C18f）。

## 九、AI 输出不是 StrategyCandidate

这是本轮最重要的边界。provider **不允许**输出 `candidate_id` /
`candidate_fingerprint` / `candidate_schema_version` / parent checksum / `batch_id` /
`proposal_id` / `status` / `score` / `rank` / `promotion`。

AI 只输出一个 bounded candidate proposal，然后：

```text
AI Proposal → R35-B CandidateSearchSpace → R35-B StrategyCandidate
```

Candidate identity 仍然只有 `strategy_candidate` 一个 authority。

## 十、Provider 严格 schema 与禁止字段

顶层只允许 `parameter_variants` + `factor_slot` / `entry_slot` / `exit_slot`。
出现以下任一字段即 `invalid_proposal_field`，**不是**静默忽略：

```text
strategy_id / strategy_version / strategy_checksum       ← parent
candidate_id / candidate_fingerprint / candidate_schema_version
asof / universe_spec / intended_market_regime / constraints
generator_type / generator_version / generator_contract_version
max_candidates / evidence_count
hypothesis_id / research_provenance / model_identity
status / score / rank / sharpe / return / drawdown / winner
promotion / deploy / execution / order / position / risk_override
```

**越权尝试与协议漂移必须可区分**：前者是 `invalid_proposal_field`（AI 试图声明它无权
声明的字段），后者是 `unknown_proposal_field`（没人认识的字段）。C7c 逐个遍历
`FORBIDDEN_PROVIDER_FIELDS` 并要求给出前者 —— 这条断言让该集合成为**承载语义**的
边界，而不是装饰（M-AIG5 曾经 SURVIVED 正是因为两者给出了同一个 reason）。

## 十一、AI 不拥有 constraints / universe / regime

R35-B 已确立 constraints = inherit 或 only tighten。R35-C 再进一步：**AI provider 根本
不拥有 constraints 字段**。AI candidate path 默认 `constraints = exact pinned parent
constraints`。R35-C 不开放 AI risk tuning；调整 risk limits / max exposure / max weight /
max positions 必须经过独立 risk authority，不属于本轮。

同理 universe / regime 由 caller 显式提供或由 exact parent 继承，不是 AI response。

## 十二、Generator 固定为 R35-B authority

AI 不选择 generator：AI path 固定 `generator_type = bounded_combination`（R35-B registry
当前明确支持的版本）。AI 只负责"提出有限备选"，"如何展开"是 R35-B 的职责。禁止
`"use_genetic_algorithm": true` 或 `generator_type = bayesian_search` —— 搜索控制器属于 R36。

## 十三、AI-specific cap 与资源上界

```text
MAX_AI_CANDIDATES_PER_REQUEST = 32      （≤ R35-B 全局上限 128）
MAX_AI_PARAMETERS             = 8
MAX_AI_VALUES_PER_PARAMETER   = 8
MAX_AI_ALTERNATIVES_PER_SLOT  = 8
MAX_PROVIDER_TOKENS           = 1200
```

调用方只能继续**收紧**（`max_candidates=16` OK；`33/64/128` → reject）。超限
fail closed，**绝不截断** —— 这不是性能优化，而是控制 AI proposal 的爆炸半径。
C10 用 5 个合法参数值证明"超限拒绝"与"静默截断"可区分。

资源上界在**任何** AST / 字段校验之前生效：`10000` 个 alternative 不会先把禁止字段
扫描跑一遍。

## 十四、AI 不拥有 DSL 解释权

provider 返回的 AST 备选一律经既有 `strategy_dsl_schema` /
`strategy_candidate_search_space` 验证。真正的规则归 `strategy_dsl_schema`；AI proposal
contract 只验证 JSON shape / 资源上界 / 禁止字段。禁止自由源码（python / eval / exec /
import / shell / SQL / source_code / lambda）进入候选。

## 十五、禁止"自动修复"

AI 返回 unknown op / invalid parameter / risk-expanding proposal / oversized search
space → **reject**。禁止自动删除非法节点、clamp 参数、截断 alternatives、换成 parent
value、把 invalid operator 换成相近 operator —— 那会让实际候选不再等于 AI 提出的候选。

## 十六、no-op proposal 必须拒绝

如果 AI 最终提出的 search space 全部 inherit parent、无任何参数/角色变体，那就是
"没提出任何东西"：生成它等于把父策略自己当成 AI 候选，让 AI 看起来产出了候选。
直接 `no_op_proposal`。

`absent` **算**已声明的变化：它显式声明"本候选没有该角色"，与 inherit 是两件事（C11b）。
空备选的 `explicit_variant` 不算变化。

## 十七、Provenance 映射

```text
research_provenance.source_kind        = ai_research
research_provenance.source_identity    = ai_research_run:<exact id>
research_provenance.source_fingerprint = exact ai_research_runs.record_hash
research_provenance.hypothesis_id      = exact hypothesis_id
```

`source_identity` 绝不是 `"latest-ai-research"`；`source_fingerprint` 直接用 R27 已有的
`record_hash`（R27 是 integrity authority），不重新 hash 一份替代品。C13 覆盖。

**model identity 是两个概念**：R27 research run 自己已有"最初由哪个 model 产生
hypothesis"的审计；R35-C 的 `model_identity` 表示"**哪个 provider / model 把
hypothesis 转成 candidate proposal**"。两者不混用，provider 没给可靠 version 就留空、
不编造（C12b / ProviderProtocolTests）。

它记录**两件事**，且只记录仓库已经解耦过的规范槽位身份：

```text
model_identity.provider = canonical slot identity（ai1 / ai2）
model_identity.model    = provider 自报的 model
```

`provider` 必须记：`ai1` 与 `ai2` 可能配**同一个** model。若只记 model，两个槽位提出
完全相同 proposal 时"哪个 provider 提出的"在台账上无法回答，且 `model_identity` 是
search-space provenance 的一部分，`generation_input_fingerprint` 也会相同 —— 输入事实的
差异被抹平。这里保存 canonical slot（`ai1` / `ai2`）而**不是**厂商名：厂商耦合已在
R27 由 `resolve_slot` 消除。`MODEL_IDENTITY_KEYS` 本来就允许 `provider`，因此不需要
新 schema。C12c/C12d + M-AIG16 覆盖。

## 十八、Evidence count 不允许 AI 自述

`evidence_count` 从 exact research run 的 canonical hypothesis 投影派生
（`len(hypothesis.evidence)`），AI 无权返回 `"evidence_count": 100`（拒绝该字段）。
它是**输入**事实，进 generation input 指纹。

## 十九、网络请求绝不在 SQLite 写事务里

```text
1. 短读    exact research run + exact parent pin facts
2. 关闭读连接
3. AI provider 网络调用            ← 绝不持有写锁等 LLM
4. 严格解析 + R35-B search-space 校验
5. 短写    重新 pin exact parent → R35-B 生成 + 追加 batch/proposals/candidates
6. commit
```

绝不出现 `BEGIN IMMEDIATE → 等 LLM 30 秒 → INSERT`（那会持有 SQLite 锁）。C14 在
provider 调用**期间**从另一条连接取写锁，取得到才说明调用方没有持有它。

## 二十、失败语义

* **provider 失败零写入**：timeout / HTTP error / invalid JSON / unknown fields /
  invalid DSL / oversized proposal / unsupported research / parent checksum mismatch
  → batch / proposal / candidate delta 全为 0。已有旧 candidate 也不会被算成本次成功
  结果（C15 / C15b）。
* **写事务 all-or-nothing**：第 N 个 proposal 写失败 → 整次 generation batch rollback，
  不留 batch row 已有 / candidate 1 已有 / candidate 2 没有（C16）。

## 二十一、不新增第四套 Candidate Ledger

现有三张表已是 canonical owner，R35-C 产物继续写进去：

```text
strategy_candidates
strategy_candidate_proposals
strategy_candidate_generation_batches
```

**零新 AI DB 表**：不建 `strategy_ai_candidates` / `ai_generated_strategies` /
`llm_candidate_table` / `ai_strategy_proposals_db` / `strategy_ai_proposal_runs`。
AI proposal 不需要自己的表：持久审计链已经完整。

## 二十二、Generation batch 完整审计 search space

只有 `search_space_fingerprint` 对人工审计已经不够：它只能回答"是不是这个空间"，不能
回答"这个空间长什么样"。因此 batch 的 `batch_json` 持久化：

```text
canonical search-space material（search_space.projection()）
+ 调用方的额外材料（AI proposal 投影 / research_run_id / proposal contract 版本）
+ search_space_fingerprint
```

并可重算校验 `hash(canonical(material)) == search_space_fingerprint`
（`SCV.verify_batch_search_space_material`）。不建新表，直接增强既有 batch。

历史 R35-A/B batch 没有完整 material：**保持 legacy 状态、不回填**，也不凭 candidate
反推旧搜索空间（C17c）。

## 二十三、API

```text
POST /api/strategies/{strategy_id}/candidate-generations/ai
```

不造 `POST /ai-strategy` / `/generate-strategy` / `/optimize-strategy`。

请求：`strategy_version` / `strategy_checksum` / `research_run_id` / `asof` /
可选 `universe_spec` / `intended_market_regime` / `provider_slot` /
`max_candidates <= 32`。

请求体**刻意更窄**，没有 `generator_type` / `constraints` / `research_provenance` /
`model_identity` / `hypothesis_id` / `evidence_count`，客户端也无法提交 `api_key` /
`base_url` / `Authorization` —— provider 配置复用既有 `ai_review_service` 槽位 authority。

route 只做四件事：typed 请求、provider 配置解析、调用 orchestration service、错误映射。
**不**在 route 里做 research gate / parent 查找 / prompt 组装 / AST 校验 / 候选展开 /
DB INSERT（C18 与 `test_route_does_no_business_logic` 覆盖）。

拒绝映射：不存在 404、历史研究不自洽 409、请求越界与 search space 被拒 400、provider
协议/transport 失败 502。这条路径**不**返回 5xx。

## 二十四、前端（最小能力）

只做：选择/输入 exact research run、发起生成、显示 `research_run_id` /
`hypothesis_id` / research status / provider/model / parent pin / search-space
fingerprint / generation batch id / candidate count / candidate ids。

禁止：AI 推荐策略、最佳策略、最优参数、预计收益、AI Score、一键上线、Promote、Deploy。
还没有 evaluation，前端就绝不能自己"推荐"。

## 二十五、不自动触发

只做 explicit / manual invocation。不加 scheduler / cron / on every research run /
background auto generation —— 自动批量搜索属于 R36。

## 二十六、不改正式策略

AI 产物只能成为 `StrategyCandidate`。禁止直接修改 `tq_breakout` / `main_force_top10` /
正式 entry / exit / factor / parameters / constraints / allocation / risk / execution。

## 二十七、测试矩阵

`backend/test_r35c_ai_candidate_generation.py`（C1–C18）+
`backend/test_r35c_ai_candidate_http.py`（HTTP 契约）。

```text
C1  exact research run only（不存在 → reject；无 latest fallback）
C2  unsupported / insufficient 在 provider 之前拒绝（provider calls = 0）
C3  confidence 不参与资格（0.1 与 0.9 逻辑一致）
C4  asof mismatch 在 provider 之前拒绝
C5  AI 不能选 parent
C6  AI 不能控制 constraints / universe / regime / asof
C7  权威与评分字段拒绝；未知字段拒绝且与越权字段可区分
C8  非法 DSL 拒绝（python / eval / unknown op / 自由源码）
C9  非法参数由既有 parameter schema 拒绝（未声明 / 越界 / 超步长）
C10 AI cap：32 内 OK，超限 reject 不截断，请求上限不得放宽
C11 no-op proposal 拒绝（absent 算变化）
C12 跨 model 同语义仍 candidate dedup，且各自保留 model provenance
C13 provenance 精确绑定 exact run + record_hash；换 run 则 input fingerprint 变化
C14 网络调用不占写事务
C15 provider 失败零写入（且不重算旧候选）
C16 写失败整批 rollback
C17 batch 持久化 search space 可自验；篡改不通过；legacy 保持 unknown
C18 无 authority 依赖 / 无第二套 transport / 零新 AI 表 / 无新增 facade
    supported 不上调为 approved / prompt 不把 parent 当选项 / research 文本声明为数据
    凭据不进 prompt
```

## 二十八、Mutation

`work/r35c_ai_candidate_mutation_check.py`：`M-AIG1`…`M-AIG10`（含 `M-AIG5b`）全部
DETECTED，`survived = 0` / `fake = 0` / `timeout = 0`，restore SHA256 PASS，恢复后
基线 GREEN。ID 前缀 `M-AIG` 刻意避开既有 `M-B1`…`M-B20`（R34-B allocation）与
`M-G`（R35-A）/ `M-X`（R35-B）。

## 二十九、明确不属于 R35-C

candidate backtest、Sharpe ranking、best candidate、walk-forward search controller、
Bayesian optimisation、evolutionary / genetic search、automatic parameter search loop、
automatic retry based on performance、automatic candidate elimination、PIT scheduler、
robustness scheduler、winner selection、shadow / paper / production-sim activation、
promotion、retirement、Learning Store feedback、closed-loop regeneration。

这些属于 **R36 Experiment Search Controller** 与 **R37 Closed-loop Learning /
Autonomous Strategy Factory**。

## 三十、non-blocking follow-up（不在本轮）

`candidate_from_projection` 最终应只接受明确支持的 v1 / v2 schema、未知 schema
fail closed。R35-C 完全不需要修改 candidate deserialization，因此**不顺手扩大本 PR**，
记录为 follow-up。
