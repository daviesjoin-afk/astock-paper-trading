# R27-B2B —— Legacy Research Runtime Inventory

本文件是 **审计产物**，不是设计提案。它回答一个问题：

> 现有 legacy research runtime 到底由谁负责，怎样逐条迁移到 R27 typed research path？

范围：`backend/` 下所有会在生产里触发、执行或持久化"AI 研究"的路径。
所有行号以 R27-B2A 合并后的 `master` 为基线；R27-B2B 改动的行号以本 PR 的 HEAD 为准。

---

## 一、Production entrypoint → 网络 / 写入

### 定时入口（宿主 cron）

| 触发 | runner | 函数 | 网络？ | 写入的 legacy 表 |
| --- | --- | --- | --- | --- |
| `--session midday` | `adaptive_runner.py:144` | `adaptive_engine.run_midday_observation` | **否** | 仅 `adaptive_runs` |
| `--session midday-advisor` | `adaptive_runner.py:146` | `adaptive_engine.run_midday_advisor` | **是** | `adaptive_advisor_runs` + `adaptive_ai_tuning_runs` |
| 收盘（含 retry） | `adaptive_runner.py:148` | `adaptive_engine.run_learning_cycle` | **是** | `adaptive_advisor_runs` + `dual_ai_tuning_runs` + `adaptive_trade_attributions` + `evolution_tracking` |

### HTTP 入口

| 路由 | 处理函数 | 网络？ | 写入的 legacy 表 |
| --- | --- | --- | --- |
| `POST /api/adaptive/ai/analyze` | `run_scheduled_ai_analysis` → `ai_analysis.run_analysis` | 是 | `adaptive_ai_analysis_runs` |
| `POST /api/adaptive/advisor/run` | `run_advisor_review` | 是 | `adaptive_advisor_runs`（`data_quality`） |
| `POST /api/adaptive/advisor/suite` | `run_advisor_suite` | 是 | `adaptive_advisor_runs`（`data_quality` + 5 个研究 purpose） |
| `POST /api/adaptive/ai/tune` | `run_ai_tuning` → `run_realtime_tuning` | 是 | `adaptive_ai_tuning_runs` |
| `POST /api/adaptive/dual-ai/tune` | `run_dual_ai_tuning_fn` → `ai_review_service.run_ai_review` | 是 | `dual_ai_tuning_runs` |
| `POST /api/adaptive/run` | `adaptive_learning_dispatch.enqueue` → worker → `run_learning_cycle` | 是（异步） | 同收盘周期 |
| `POST /api/adaptive/tuner/apply` | `evolution_apply.apply_tuner_proposals` | 否 | **`paper_accounts.params`** + `dual_ai_tuning_runs.applied_ids` |

读（展示）入口：`/api/adaptive/overview`、`/ai/overview`、`/ai/timeline`、`/dual-ai/status`、`/dual-ai/runs`、`/evolution/status`。前端消费点在 `frontend/src/features/adaptive.js`。

---

## 二、逐题回答

### 1. 哪些 production entrypoint 会触发 research？

`run_midday_advisor`、`run_learning_cycle`、`run_advisor_review`、`run_advisor_suite`，以及 `POST /ai/analyze`（`ai_analysis`）。它们全部落在 `adaptive_engine` 的四个公开函数与 `api_adaptive` 的路由函数上。

### 2. 哪些路径会真正发 LLM / network request？

**整个仓库只有两个生产模块持有 AI 的 HTTP 调用**：

- `ai_provider_transport.py:286` —— `urllib.request.urlopen`，R27-B1 起是 **canonical** provider 网络 owner（AI provider 的 HTTP 调用只允许出现在这里）。
- `deepseek_advisor.py:486` —— legacy provider owner（`call_json`）。

其余全部委托，无自有网络调用：`deepseek_research`（`advisor.call_json`）、`ai_analysis`（`provider_module.call_json`）、`ai_review_service`（`transport.call_json`）、`dual_ai_tuner`（facade → `ai_review_service`）、`trade_attribution`（`deepseek_advisor.call_json`）、`ai_research_provider`（`transport.call_json`）。

所以 R27-B2B 之前存在**两个** AI 网络 owner；canonical 的那个必须保持唯一。

### 3. 哪些路径会写 legacy AI / research 表？

| 表 | 行含意 | production writer |
| --- | --- | --- |
| `adaptive_advisor_runs` | 一次证据复核。列：`purpose/trigger/status/provider/model/evidence_hash/evidence/report/...` | `deepseek_advisor.run_review`（`purpose='data_quality'`）与 `deepseek_research._save_run`（5 个研究 purpose） |
| `adaptive_ai_tuning_runs` | 一次有界调参尝试（`purpose='bounded_tuning'`） | `deepseek_advisor._tuning_failure_row` / 成功写入路径 |
| `adaptive_ai_analysis_runs` | 一次时间窗分析，按 `business_key` 幂等 | `ai_analysis.run_analysis`（INSERT + 三次 UPDATE） |
| `dual_ai_tuning_runs` | 一次 AI 评审（单/双 reviewer）与其共识 | `ai_review_service._write_audit` |
| `ai_research_runs` | canonical typed research run（append-only） | `ai_research_repository.append_run`（R27-B2A 之前**没有** production 调用方） |

### 4. 哪些结果现在会被谁消费？

- `adaptive_advisor_runs`：`deepseek_advisor.overview`（展示，经 `adaptive_engine._overview_uncached` 进入 `/ai/overview`）；`deepseek_research._latest_data_quality`（**作为下一条研究任务的输入证据**）。
- `adaptive_ai_tuning_runs`：`run_realtime_tuning` 的冷却门禁与连续拦截告警；`overview` 展示。
- `adaptive_ai_analysis_runs`：`ai_analysis.run_analysis` 自身的幂等/租约判断；`ai_analysis.timeline` → `/ai/timeline`。
- `dual_ai_tuning_runs`：`evolution_apply.apply_tuner_proposals`（**apply 门禁**）、`evolution_apply.rollback_tuner_overlay`、`adaptive_engine._tuner_effectiveness`（奖励归因）、`ai_review_service.recent_runs` → `/dual-ai/runs`、`self_evolution` 的外键。
- scheduler：cron 只**触发** runner，不读这些表。

### 5. 哪些 legacy row 是纯 audit？

`adaptive_ai_analysis_runs` 整表（只被幂等判断与 `/ai/timeline` 读）；`adaptive_ai_tuning_runs`（审计 + 运行期门禁，`auto_apply=False`，可执行状态在 `adaptive_selection_candidates`）；`adaptive_advisor_runs` 的 `data_quality` purpose（展示 + 作为下一条研究的输入证据，**不**是决策输入）。

**不是**纯 audit：`dual_ai_tuning_runs`（`status='consensus'` + `merged_proposals` 是 apply 门禁的一部分）。

### 6. 哪些 legacy result 会实际影响 strategy / proposal / risk / signal / portfolio？

- **strategy config：唯一一条 legacy AI → 变更的路径**是 `dual_ai_tuning_runs.merged_proposals` → `evolution_apply.apply_tuner_proposals` → 写 `paper_accounts.params["adaptive_selection"]`。
- **tuner proposal**：`run_realtime_tuning` 提出 `adaptive_selection_candidates`（`status='shadow_proposal'`），必须经人工 `POST /selection/apply` 才生效。
- **reward attribution**：`dual_ai_tuning_runs.applied_ids` → `adaptive_engine._tuner_effectiveness`（元数据）。
- **risk / signal / portfolio：没有任何 legacy AI 行影响它们。**
- `adaptive_advisor_runs`、`adaptive_ai_analysis_runs`、`adaptive_ai_tuning_runs` 都**不**流入 apply。

### 7. 是否存在"typed provider 调一次 + legacy provider 又调一次"的重复付费路径？

**迁移前不存在**（因为没有 production 调用方走 typed provider）。但存在**潜在**风险：一次收盘周期已经串起多次付费调用（成交归因、双 AI 评审、收盘复核、研究套件、调参），把 typed provider 接进这条链而**不删除**对应 legacy 调用，就只是**增加**一次付费。这正是本轮必须"迁移即停止 legacy writer"、而不是"并行接一条新路"的原因。

### 8. 是否存在 canonical 写一份 + legacy dual-write 一份的风险？

**迁移前不存在**（canonical 唯一 writer `ai_research_repository.append_run` 无生产调用方）。本轮迁移后也**必须不出现**：`run_review` 的 legacy INSERT 被删除，而不是被保留。

---

## 三、R27 typed path 的现状接口（迁移必须依赖的事实）

- `ai_research_contract.SUPPORTED_OWNER_ADAPTERS = frozenset({"market_data"})` —— **目前只有市场数据有可签发的 typed owner adapter**。
- `ResearchEvidenceRef` 没有公开构造器；唯一签发路径是 `evidence_ref_from_market_reading(reading)`，且它要求一个真正的 `market_data_contract.MarketDataReading`。
- `ai_research_provider.run_research(*, provider_config, hypothesis_id, as_of, subject, question, events, max_tokens)`：不碰 DB、不读时钟、不自己联网（委托 `ai_provider_transport`）。
- `ai_research_repository.append_run(conn, *, hypothesis, purpose, trigger, created_at, ...)`：只接受 typed hypothesis；`status` / `reason` / `authority` / `is_authoritative` 不是参数。

**这直接决定了一条硬边界**：任何 legacy research runtime 的证据若**不是**市场数据，就不可能在不伪造 provenance 的前提下迁进 typed path。

### provider 配置权威（迁移后的事实）

profiles / 凭据也有一条同类边界：

| 谁 | 判据 | 管什么 |
| --- | --- | --- |
| `ai_review_service.slot_readiness(cfg)` | Key + `enabled` + 可请求地址 + 模型 | **已迁移的** canonical research |
| `deepseek_advisor.configured()` | 厂商环境变量（`DEEPSEEK_API_KEY` 等） | **未迁移**的 tuner / 研究套件 |

两者刻意不共用判据 —— 它们问的是两个不同的 provider owner 能不能付钱。这条边界由两个方向的
真实缺陷写下来：用 legacy 环境变量当准入条件会让「只在 `ai_provider_slots` / UI 里配好的槽位」
永远跑不起来；不检查 `enabled` 则让操作员的 disable 只挡住 UI、挡不住真实付费调用（因为
`ai_provider_transport.call_json` 只看 `api_key` / `base_url` / `model`）。

---

## 四、迁移决策：Step 1 为什么是 `data_quality` 运行时

legacy 研究运行的证据构成：

| runtime | 证据 | 能否 type 化 |
| --- | --- | --- |
| `deepseek_advisor.run_review`（`data_quality`） | **R24 authority 的全市场快照**（`market_data_service.read_snapshot_with_meta`） | **能** |
| `deepseek_research.run_task/run_suite` | 模拟盘净值/成交、adaptive 候选、新闻事件、事故统计 | **不能**（无 owner adapter） |
| `ai_analysis.run_analysis` | 市场快照（可 type）+ 模拟盘上下文（不可 type）+ `business_key` 幂等生命周期 | 部分 |
| `dual_ai_tuner` / `ai_review_service` | 账户参数与候选 | 不能，且它属于 **proposal / apply 边界**，不是 research |

因此：

- `deepseek_research` **不能**在本轮迁移 —— 把 paper/adaptive/news 事实塞进市场事实的 payload 会伪造 provenance，而给它们新增 owner adapter 是 R27-A 的契约工作，不是 B2B 的接线工作。它的 writer 计数本轮**不变**。
- `ai_analysis` **不能**整体迁移 —— 它的 `business_key` + INSERT/UPDATE 生命周期与 append-only 台账冲突，且它的前端 timeline 读路径属于 R27-B3。
- **`deepseek_advisor.run_review`（`data_quality`）是唯一证据已经类型化的 legacy research runtime**，因此是本轮的 Step 1。

### 实际迁移

```
adaptive_engine（4 个 production 调用点）
        ↓
deepseek_advisor.run_review                       ← 迁移后的 runtime
        ↓  从 R24 authority 取 typed MarketDataReading
        ↓  建成 typed InformationEvent
ai_research_service.run_research_run              ← 单一 orchestration boundary
        ├── ai_research_provider.run_research     ← provider 恰好一次（不在事务内）
        └── ai_research_repository.append_run     ← append 恰好一次（短事务）
        ↓
ai_research_runs                                  ← canonical ledger
```

`run_review` 的返回键保持不变（`id` / `status` / `report` / `error_code` / `latency_ms`），因此四个调用方与 `overview` 的接线不需要跟着改。

### 能力收窄（刻意，且必须被记录）

legacy 的 `data_quality` 证据里还混着**未类型化**的事实：模拟盘账本对账（孤儿成交、负现金、委托/成交不一致）。把账本事实写进市场事实的 payload 会伪造 provenance，因此它们**不进入**这次研究。这些确定性检查仍然保留在 `collect_evidence` 中，仍被 tuner 门禁与展示路径使用。

由此产生两条 README 级可观察变化：

1. 迁移后 `data_quality` 研究执行的是**市场数据质量**这一件事，判定依据从"模型自述严重度"变成"R27-A 从 R24 核验维度派生的 status"。
2. `deepseek_research._latest_data_quality`（`incident_triage` 的输入证据）改读 canonical 台账，因此它送给模型的输入从"上一轮证据聚合"变成"上一轮的研究推理"。

### 读侧收敛（两处，read-only）

writer 迁走之后，旧的读路径若不收敛，`/ai/overview` 会停在历史行、`incident_triage` 会静默失去输入。本轮只做**读投影**收敛，不写第二份数据：

- `deepseek_advisor.overview`：`data_quality` 这个 purpose 以 canonical 为准；旧行仍然可见并被标记 `source="legacy_adaptive_advisor_runs"`。
- `deepseek_research._latest_data_quality`：改读 canonical 台账。
- 两者的读入口合并在 `deepseek_advisor.latest_data_quality_research`（一处定义，避免漂移）。
- `overview` 的 legacy 形状兼容投影 `_canonical_research_display` 有**明确删除条件**：R27-B3 提供 canonical research API / UI 时随调用点一起删除。

---

## 五、未迁移清单与删除条件

| 未迁移对象 | 原因 | 删除 / 迁移条件 |
| --- | --- | --- |
| `deepseek_research._save_run` → `adaptive_advisor_runs`（5 个 purpose） | 证据未类型化（paper / adaptive / news 无 owner adapter） | 需要 R27-A 新增真实 owner adapter（不是本轮的接线工作） |
| `ai_analysis.run_analysis` → `adaptive_ai_analysis_runs` | `business_key` + 生命周期与 append-only 冲突；前端 timeline 属 R27-B3 | 先由 application 层定义幂等业务键，再做 writer 迁移 + 读侧收敛 |
| `deepseek_advisor.run_realtime_tuning` / `_tuning_*` → `adaptive_ai_tuning_runs`、`adaptive_selection_candidates` | 属于 **proposal** 边界，不是 research；顺手改会同时改变它的业务 authority | 单独一轮；必须先画出 `research output → proposal boundary → deterministic apply` |
| `ai_review_service._write_audit` → `dual_ai_tuning_runs` | 是 `evolution_apply` 的 **apply 门禁**，改动会波及策略配置变更 | 单独一轮；必须同时保留 `applied_ids` 与 `status='consensus'` 两个消费者 |
| `deepseek_advisor.call_json`（legacy provider 网络 owner） | 仍被 tuner 与 `trade_attribution` 使用 | 随上述三条一起消失；在此之前它必须留在"未迁移"清单里，而不是被静默复制 |

---

## 六、本轮结论（before / after）

```
Business authority added:              0
Business authority removed:            0
Legacy research writer sites:          before = 2   after = 1        （data_quality 迁走）
Canonical research writer sites:       before = 1   after = 1
Direct AI network owners:              before = 2   after = 2        （canonical 仍唯一）
Legacy runtime paths migrated:         1
Legacy runtime paths remaining:        4
Dual-write path:                       NO
Historical legacy rows migrated:       0
Signal/order/risk/promotion writer added: 0
Implicit current-state lookup added:   0
New facade/wrapper:                    1  （overview 的 legacy 形状兼容投影，有删除条件）
Old compatibility path removed:        1  （run_review 的 legacy 写入路径）
核心 research runtime 需要查看的 production modules: before = 6  after = 7
```

最后一项变多是因为新增了 orchestration boundary 本身；这是**拆分**而不是扩张：迁移前"由谁发起、用谁付费、写哪张表"散落在 runtime 里，现在集中在一处。
