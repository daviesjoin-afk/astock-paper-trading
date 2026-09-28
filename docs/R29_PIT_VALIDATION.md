# R29 — Canonical Point-in-Time Validation Lab

## 阶段状态

| 阶段 | 状态 | 边界 |
| --- | --- | --- |
| R28-A | COMPLETE | 固定不可变 Experiment identity 和 canonical result contract。 |
| R29-A | COMPLETE | PR #212 提供 fail-closed PIT evidence gate。 |
| R29-FINAL | COMPLETE | 增加 canonical archives、validation runner、append-only ledger、API 与 Research Workspace consumer。 |
| R30 | NOT STARTED | 不包含 robustness、扰动实验或策略晋级。 |

`R29 = COMPLETE` 表示代码具备完整、可复核的 authority path；不表示本地已经装有覆盖所有历史日期的市场、证券或财务数据。缺少归档或 PIT 证据的具体 run 仍返回 `unavailable` / `BLOCKED`，绝不补零或回退到当前数据。

## R29 canonical authority matrix

| Dimension | Declared identity | Canonical owner | Immutable? | PIT provenance / coverage | Runner consumer | Current or legacy surface | Final treatment |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Historical market | `ExperimentSpec.market_data_fingerprint` | `HistoricalMarketArchiveRepository` | 是；manifest 与 bars append-only，按 raw 内容寻址 | 明确 source/revision；有限日期和证券范围读取；缺 bar 需逐 session 的 tradability 事实解释 | PIT gate 与 deterministic replay | current K-line、qfq cache 不进入 canonical archive | CANONICAL OWNER |
| Session calendar | `validation_calendar_fingerprint` | `historical_session_calendar.issue_from_market_archive` | 是；projection 由 exact raw benchmark archive 派生 | typed owner-issued projection，含完整请求范围、session list、来源 archive 和内容 hash | PIT gate 与唯一 walk-forward splitter | live/chinese calendar 不作历史 authority | CANONICAL OWNER |
| Historical universe | `ExperimentSpec.universe_fingerprint` | `HistoricalUniverseArchiveRepository` | 是；manifest 与 security master rows append-only | exact manifest 覆盖日期范围；逐 session 通过 `point_in_time.historical_universe` 求成员 | PIT gate 与 replay membership | `universe.json` 仍是 current snapshot | CANONICAL OWNER |
| Tradability | `ExperimentSpec.tradability_fingerprint` | 既有 `TradabilityArchiveRepository` | 是；复用现有 owner | 单个 SQLite read snapshot 同时选择 15:00 close 与 09:30 execution facts；unknown 大于 0 时阻断 | PIT gate 与 deterministic replay 共用同一次 capture | current 状态不回填过去 | REUSE EXISTING OWNER |
| Fundamental PIT | dataset fingerprint + financial archive fingerprint + sample/field evidence refs | `HistoricalFinancialArchiveRepository` 保存披露记录；`FinancialFeatureEvidenceRepository` 绑定 feature、sample、输入记录、派生版本和值 | 是；archive 与 evidence append-only | 复用 `financial_point_in_time.financial_visibility`；按精确 decision instant；feature refs 纳入 dataset fingerprint；旧 dataset 不回填 | PIT gate 与 financial DSL snapshot | caller record、通用 `pit_status`、current financial endpoint 均无 authority | CANONICAL OWNER |
| Strategy | `strategy_id + version + checksum` | `strategy_registry.StrategyVersion` + immutable DSL AST | 是 | exact identity 三项匹配；只执行版本化 DSL AST | dependency analysis 与 DSL evaluator | current `strategies.py` implementation 不作历史回放 | CANONICAL CONSUMER |
| Dataset | `ExperimentSpec.dataset_fingerprint` | `learning_dataset` manifest | 是 | exact manifest fingerprint；financial lineage refs 属于 identity | PIT gate 与 runner | 禁止 latest/current fallback | CANONICAL CONSUMER |
| Walk-forward | ExperimentSpec date range + explicit as-of | `walk_forward_validation.build_walk_forward_folds` | 确定性投影 | 只消费 owner-issued sessions；保留 label availability purge 和 session embargo | runner | 不创建第二套 splitter | REUSE EXISTING OWNER |
| Execution / cost | ExperimentSpec pinned assumptions and cost model | `experiment_execution_model` interprets declared assumptions | 结果确定且由 spec 固定 | next-open、T+1、历史 tradability、volume capacity、commission、tax、slippage 均来自精确 spec/archives | runner | production execution authority 仍是 R26；不读 paper runtime config 或 `backtest.py` 常量 | CANONICAL CONSUMER |
| Run ledger | experiment fingerprint + exact owner identities + runner version | `ExperimentValidationRepository` | 是；同 key 同 payload 幂等，不同 payload 冲突 | append-only; corrupt JSON/hash fails closed | API list/detail | 不覆盖或替换 canonical result | CANONICAL OWNER |
| API / UI | exact ExperimentSpec and archive identities | adaptive canonical experiment routes + Research Workspace | ledger-backed | POST offline；GET 不运行实验；UI 显示 backend status、period、windows、coverage、warnings 与 result | operator read/submit surface | 不使用 legacy backtest projection，不排序或晋级策略 | CANONICAL CONSUMER |

## Canonical chain and fail-closed semantics

```text
exact ExperimentSpec and immutable identities
  → market / calendar / universe / tradability / financial owners
  → PITValidationEvidence
  → pinned strategy DSL + declared execution model
  → ExperimentResult
  → append-only experiment_validation_runs
  → canonical API
  → Research Workspace
```

- Raw market import 拒绝 qfq/adjusted data；ingestion 与 experiment execution 分离。runner 不联网、不调用 provider，也不刷新历史。
- Calendar 只能从 exact raw benchmark archive 签发 typed projection；caller Mapping、samples、weekday guess、current calendar 不能声明完整历史范围。
- Universe 证明必须经过 exact historical archive owner；caller 提供的 membership rows/source mapping 不能签发 universe proof。历史退市股票在其真实有效范围内保留。
- Fundamental observations 必须来自归档 owner，并绑定实际 `sample_key`、feature、精确 decision instant、输入披露和 derivation version。date-only decision 不证明盘中时点；同日日期粒度披露对盘中决策 fail closed。
- 只有 required PIT dimensions 全部 proven 且至少一个 walk-forward fold ready，runner 才开始 replay；否则 result 为 `unavailable`，无性能指标。
- `failed` 表示 deterministic replay 真正执行后失败；`unavailable` 表示身份、owner 或 PIT 输入不足。unknown 不转成 0。
- Ledger key 确定性绑定 Experiment、archive identities、strategy、dataset、evidence 与 runner version；`created_at` 不进入身份。
- 前端只显示 canonical backend semantics。不存在 approval、promotable、champion、winner 或单一 magic score。

## API and Research Workspace

- `POST /api/adaptive/experiments/validate`：只接收 exact ExperimentSpec、owner fingerprints 和 walk-forward 配置；offline execution。
- `GET /api/adaptive/experiments/runs`：限制最多 200 条，按 append order 读取。
- `GET /api/adaptive/experiments/runs/{run_id}`：只查 ledger；corrupt rows fail closed。
- Research Workspace 可查看 Experiment / Run / Evidence / Result，并比较两条持久化 run；显示 code revision、dataset/archive fingerprints、Train / Validation / OOS、walk-forward windows、每类 data coverage 与 PIT warnings。
- 未知指标显示 unavailable；UI 不推断成功或晋级，也不把 unavailable 渲染成 0。

## 数据准备不等于能力缺失

下面这些情况仍可使某一场实验 `BLOCKED / unavailable`，但不影响 R29 代码能力完成：raw benchmark archive 缺失或覆盖有缺口、historical security master 未导入、tradability facts 不完整、指定金融字段没有可见的历史披露记录、指定 dataset 没有带金融 lineage refs。所有情形继续 fail closed。

以下相邻阶段问题继续按各自 owner 管理，不由 R29 自动关闭：

- adaptive killed terminal instant
- non-intraday paper job stable attempt identity
- `adaptive_execution_evidence` metric/status semantics
- adaptive rewards historical availability
- alpha candidate stable identity
- historical mutable candidate revisions
- parameter/experiment linkage
- historical cycle membership
- physical DB origin / trusted provenance outside the R29 archive import boundary

R29 不修改 legacy `backend/backtest.py`，不做 R30 robustness、bull/bear stress、cost shock、parameter/date/universe perturbation、strategy selection/promotion、AI scoring、paper promotion、真实交易、broker 或 deployment。
