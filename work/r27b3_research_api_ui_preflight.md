# R27-B3 前置工作：canonical research API/UI 契约与迁移清单

状态：B3 实施基线审计；契约已实现，最终验证与 PR 审核待完成。

基线：R27-B2C-FINAL merge commit `9520cf446bd619c758484d520f9a68d79ca1b520`。

## 目标与边界

B3 的仓库定义是提供 canonical research API/UI，并在新读路径接管后删除 B2B 的 legacy 形状兼容投影。canonical owner 是 `ai_research_runs`，读写入口分别为 `ai_research_repository.recent_runs()`、`get_run()` 和唯一 append writer `append_run()`。

Research run 是历史研究产物，不是当前市场事实，也不是交易、风控或策略晋级授权。API/UI 必须保留这个边界；证据缺失、记录损坏或读取失败不得显示成“无研究结果”或成功结论。

## 现状盘点

- `backend/ai_research_repository.py` 的读取投影已校验 JSON 形状、重复字段自洽和 `record_hash`；损坏时抛出 `ResearchPersistenceError(reason="corrupt_research_record")`。`recent_runs()` 支持 `limit`、`as_of`、`subject`、`purpose` 等值过滤，按自增 `id DESC` 排序，单次上限为 200；`get_run()` 按 id 读取，查无记录返回 `None`。
- `backend/api_adaptive.py` 当前只有 `/api/adaptive/ai/overview`、`/api/adaptive/ai/timeline` 和 `/api/adaptive/ai/analyze` 相关入口，没有面向 canonical ledger 的列表/详情 API。
- `/api/adaptive/ai/timeline` 读取 `adaptive_ai_analysis_runs`，表达时间窗运行状态、重试和 canonical run 引用。它是 operational timeline，不等同于 canonical research history。
- `backend/deepseek_advisor.py` 的 `_canonical_research_display()` 把 canonical row 投影成 legacy `advisor.overview` 形状；其 docstring 明确要求 B3 新 API/UI 接管后删除函数及调用点。
- `frontend/src/features/adaptive.js` 的 `refreshAdaptiveTimeline()` 读取 `/api/adaptive/ai/timeline`，同时接受 overview payload 的旧 fallback 形状。时间线卡片主要表达运行状态、模型路由、证据哈希、重试与人工确认。
- `ai_research_repository` 已提供可复用的验证读取函数，无需另建 ledger 或直接在 API 层写 SQL。

## Compatibility surface 审计矩阵（B3 开始前）

| SURFACE | CURRENT PRODUCER | CURRENT CONSUMER | CANONICAL REPLACEMENT | AUTHORITY | B3 TREATMENT |
|---|---|---|---|---|---|
| `adaptive_advisor_runs` | R27-B2C 后 production research writers = 0；B3 删除了 `ensure_schema()` 的空表和索引创建 | B3 前 `deepseek_advisor.overview()` 中唯一一处历史查询；B3 后生产读者 = 0 | `/api/adaptive/research/runs` | 仅 canonical ledger 有 research authority；此表仅历史 | HISTORICAL TABLE LEFT PHYSICALLY UNUSED |
| `adaptive_ai_analysis_runs` | `ai_analysis.run_analysis()` 写运行/幂等状态和 canonical run reference | `ai_analysis.timeline()`、`/api/adaptive/ai/timeline`、timeline panel | canonical result 详情由 `/api/adaptive/research/runs/{run_id}` 提供 | operational only | KEEP OPERATIONAL |
| `_canonical_research_display` | `latest_data_quality_research()` 的 canonical row | `deepseek_advisor.overview()` → `latest` / `latest_by_purpose` → adaptive UI | canonical list/detail API | `ai_research_runs` | REMOVE COMPATIBILITY READ |
| `research_report_view_from_row` | repository `recent_runs()` / `get_run()` 的 validated row | `ai_analysis` 幂等执行响应；`deepseek_research._latest_data_quality()` 研究上下文 | canonical API 对外返回原始 canonical projection；runtime report response 仍使用此投影 | research only | KEEP CANONICAL |
| `latest_data_quality_research` | `ai_research_repository.recent_runs()`，purpose 精确过滤 | `deepseek_research._latest_data_quality()`；overview 兼容投影调用 | research context 留用；API 列表复用 repository | canonical ledger | KEEP CANONICAL |
| `deepseek_advisor.overview()` | tuning tables + `adaptive_advisor_runs` 历史读 + canonical data-quality lookup | adaptive overview、前端 tuning 和旧 research widgets | overview 留 operational/tuning 数据；research history 由独立 API | tuning 状态各有 owner；research 仅 canonical ledger | REMOVE COMPATIBILITY READ |
| `deepseek_research._latest_data_quality()` | `latest_data_quality_research()` | 后续 research collector 的 prior-run context | 原 canonical research context path | historical research context only | KEEP CANONICAL |
| `ai_analysis.timeline()` | `adaptive_ai_analysis_runs` operational rows | `/api/adaptive/ai/timeline` 与前端 operational timeline | timeline 返回状态字段和 `canonical_run_id`；详情单独走 canonical API | operational only | KEEP OPERATIONAL |
| frontend `deepseek.latest` / `latest_by_purpose` / `advisorLatest` / `report` | `deepseek_advisor.overview()` compatibility shape | research task cards、data-quality report summary/findings | canonical history API list/detail | canonical research ledger | MIGRATE UI |
| frontend `canonical_research` | timeline 中通过 `repository.get_run()` 嵌入的 conclusion | 旧 timeline renderer/fallback | timeline 只给 `canonical_run_id`；history/detail 走 canonical API | canonical research ledger | REMOVE COMPATIBILITY READ |
| frontend `adaptive_ai_analysis_runs` timeline | `/api/adaptive/ai/timeline` | run status/retry timeline | 保持同一 operational API 语义 | operational only | KEEP OPERATIONAL |

审计计数：`adaptive_advisor_runs` production writers = 0，research readers = 1；唯一读点是 overview 中的历史兼容查询。`_canonical_research_display` 定义/调用面 = 1；canonical research UI API source = 0；overview legacy research source = 1。前端 `latest` 与 `latest_by_purpose` 是同一 overview payload 的两个字段，迁移时均删除，不算成两个独立 owner。

## 建议 API 契约（待实现）

### 列表（已实现）

- 建议路由：`GET /api/adaptive/research/runs`，由 canonical research router 提供，避免把 canonical owner API 放进 adaptive 专属命名空间。
- 查询：`limit`（默认 50，范围 1–200）、可选 `as_of`、`subject`、`purpose`；筛选均为精确匹配。排序固定为 `id DESC`。
- 响应：`{"status":"ok","runs":[...]}`。每个 item 使用 repository 已验证的 projection 字段，不返回数据库原始行或 legacy-shaped `report/evidence` 包装。
- 当前 UI 使用有界 `limit=50`；API 范围为 1–200。本轮不引入 cursor，也不复制或扩展 repository 查询语义。

### 详情

- 建议路由：`GET /api/adaptive/research/runs/{run_id}`。
- 存在时返回一个已验证的 canonical projection；不存在返回 404。
- 损坏记录返回稳定的错误码 `corrupt_research_record`，不泄露 SQL、raw row 或损坏 JSON，也不降级成 404/空列表。

### 公开字段与语义

- 可显示：`id`、`purpose`、`trigger`、`hypothesis_id`、`as_of`、`subject`、`status`、`reason`、`confidence`、`authority`、`is_authoritative`、`provider_slot`、`provider_model`、`hypothesis`、`narrative`、`counter_arguments`、token/latency 指标、`record_hash`、`created_at`。
- `status` / `confidence` 是当时保存的研究判断。详情与列表都应标明这是历史产物；`authority` 为 `research`、`is_authoritative` 为 false，不得渲染成事实核验或可执行授权。
- 不返回 API key、authorization、request headers、raw prompt、system/user prompt、raw provider response。不要把运行错误或缺失数据合成为空 hypothesis。

## 建议 UI 契约

- 增加 canonical “研究记录”列表，数据直接来自 canonical research API；按持久化顺序展示 purpose、业务时间 `as_of`、持久化时间 `created_at`、subject、历史 status/confidence 与结论摘要。
- 选择一条记录后读取详情，展示 narrative、反方论据、typed hypothesis 与 evidence references；展示明确的历史时间和“研究结果不构成当前事实/交易授权”提示。
- 错误状态至少区分加载失败、记录损坏和无记录；失败不能伪装成空状态。
- 保留 `/api/adaptive/ai/timeline` 的 operational 语义：它表达运行状态与重试，不替代 canonical research history。若 B3 要收敛该时间线，必须先定义并保留运行失败/blocked/重试记录的 owner 与展示语义，不能只把它改成 ledger rows。

## 迁移清单

1. 锁定 API 路由、列表上限/分页方式、字段白名单及错误响应；新增契约级 API 测试，覆盖精确过滤、稳定顺序、404、损坏记录 fail closed 与敏感字段不泄漏。
2. 使用 `recent_runs()` / `get_run()` 接入只读 API；API 首次读取时确保 schema 可用，不引入新的 SQL writer、ledger 或 DB migration。
3. 增加 canonical research history UI，显式区分 `as_of` 与 `created_at`，并覆盖加载、空、损坏/错误和详情状态。
4. 将 overview 中所有依赖 `advisor.overview` legacy-shaped research row 的 UI/调用点迁到 canonical API；核对 `deepseek_advisor.overview` 与 `_canonical_research_display()` 的全部消费者。
5. 新读路径接管后，删除 `_canonical_research_display()` 及其 overview 调用点，并移除只为该投影保留的测试/兼容字段；不得清理仍被 tuner、`data_quality` 或其他业务路径使用的 legacy 数据。
6. 单独审查 `adaptive_ai_analysis_runs` / `/api/adaptive/ai/timeline`。其 operational status 与 canonical research result 是不同数据；只有确认调用方和失败语义都有替代方案后，才能提出删除或迁移。
7. 前端完成后核对 overview、时间线与 canonical history 的 API 依赖，确保旧 fallback 不再向 canonical research UI 提供 legacy-shaped 记录。

## 保持 OPEN / REQUIRED 与 fail closed

此 B3 前置设计不关闭或重解释以下 owner 缺口：adaptive `killed` terminal instant、non-intraday paper job stable attempt identity、`adaptive_execution_evidence` metric/status semantics、adaptive rewards historical availability、alpha candidate stable identity、historical mutable candidate revisions、parameter/experiment linkage、historical market evidence gaps、historical cycle membership、physical DB origin / trusted provenance。缺失信息仍展示 unavailable，不可由 UI/API 推断补齐。

审计前态计数已记录在矩阵中；B3 实施将其收敛为两个 canonical read endpoints、一个 canonical result owner 和零个 advisor history 生产读者。该文件保留为审计轨迹，完成状态与正式语义以 `ARCHITECTURE.md` 和 R27 owner matrix 的 B3 小节为准。

