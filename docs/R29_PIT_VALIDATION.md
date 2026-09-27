# R29-A — Point-in-Time Validation Gate & Validation Evidence

## 阶段状态

| 阶段 | 状态 | 边界 |
| --- | --- | --- |
| R28-A | COMPLETE | 固定实验 identity 和 result contract；没有 runner。 |
| R29-A | IN PROGRESS | 建立 fail-closed PIT 输入证据与 READY/BLOCKED gate；以 exact-head PR 状态为准。 |
| R29 canonical runner | NOT STARTED / DEFERRED | 尚无 canonical 历史执行与持久化 read model。 |
| R30 | NOT STARTED | 本阶段不涉及 robustness。 |

`READY` 只表示已有 PIT 输入足以进入未来 canonical validation runner，不表示策略有效、可晋级或可上线。
`BLOCKED` 表示输入证据不足；它不产生零收益、零回撤或零交易数。

## R29 PIT input authority matrix

| DIMENSION | DECLARED IDENTITY | FACT OWNER | PIT AVAILABILITY OWNER | CURRENT HISTORICAL COVERAGE | CAN PROVE R29? | CURRENT FALLBACK | R29-A TREATMENT |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Historical universe | `ExperimentSpec.universe_fingerprint` | `universe` rows / future archive source | 每个实验 session 调用 `point_in_time.historical_universe` | 当前 `universe.json` 是 current snapshot；未发现完整历史成分归档 owner | 当前不能证明完整历史成分；只有 `historical_archive`、严格 complete 标志且 archive as-of 覆盖每个请求 session 才可通过 validator | current universe 和逐行 `list_date` 均不能补足源级完整性 | BLOCKED / REQUIRED |
| Historical tradability | `ExperimentSpec.tradability_fingerprint` | `TradabilityArchiveRepository` | `evidence_at` + `tradability_at` | 有双时态逐条事实，没有实验级完整覆盖清单；只请求对应 session 历史 universe 中的证券 | 仅在请求集合有完整 owner-visible facts 时可证明该集合；available、blocked、unknown 是互斥桶，缺失或未知阻断 | 不读取当前 ST、停牌、上市、成交量或涨跌停状态 | REUSE EXISTING OWNER |
| Market data PIT | `ExperimentSpec.market_data_fingerprint` | R24 market snapshot owner | `market_data_service.read_snapshot` / `market_data_contract.classify` | 只拥有当前持久化 full-market snapshot，不拥有任意历史日线归档 | 当前不能证明历史市场数据；SHA/as-of 声明不是来源或 coverage 证据 | 不把当前 snapshot 倒灌到历史；不 refresh、不联网补数 | BLOCKED / REQUIRED |
| Fundamental PIT | dataset 的精确 manifest identity | 输入财务记录来源 | `financial_point_in_time.financial_visibility` | owner 可判定显式记录的发布时间；缺披露时间的当前财务 endpoint 不能证明过去可见 | 对显式记录，publication/disclosure <= evaluation as-of 时可证明可见性；缺失/未来/非法分别报告 | report period 不代替 publication time；不使用当前最新财报回填历史 | REUSE EXISTING VALIDATOR |
| Strategy version | `strategy_id + version + checksum` | `strategy_registry.StrategyVersion` | exact version record | immutable version rows 可按显式 identity 核对 | 三字段全部与 ExperimentSpec 匹配时可证明 pinned identity | 禁止省略 version 读取 current head | REUSE EXISTING OWNER |
| Dataset | `ExperimentSpec.dataset_fingerprint` | `learning_dataset` manifest | `read_manifest(conn, fingerprint)` / exact supplied manifest | append-only manifest 可按 fingerprint 查询；`created_at` 不是 identity | 仅 manifest fingerprint 与 spec 精确匹配时可证明绑定 | 禁止 latest / `MAX(created_at)` fallback | REUSE EXISTING OWNER |
| Execution model | `ExperimentSpec.execution_assumptions` | ExperimentSpec caller boundary | R28 contract validation | 假设快照已显式固定；尚无 canonical runner 证明实际执行与声明相同 | 可证明声明字段完整；未来 runner 必须比对实际执行，偏离即 fail closed | 不读 current execution profile 或 paper 配置 | PROVEN |
| Cost model | `ExperimentSpec.cost_model`：费率、最低佣金、印花税、slippage model/parameters、version | ExperimentSpec caller boundary | R28 contract validation | 假设已显式固定；尚无 canonical cost executor | 可证明声明字段完整；不能宣称已执行成本模拟 | 不读 backtest 或 paper 全局常量 | PROVEN |
| Walk-forward | `ExperimentSpec` date range + as-of；显式 sessions | caller 提供的权威 session calendar | `walk_forward_validation.build_walk_forward_folds` | splitter 支持 explicit sessions、label maturity、purge 与 session embargo | 只把 ExperimentSpec 日期范围内的 sessions 与 samples 交给 splitter；日期范围外的项目不参与 folds，非法日期阻断；至少一个 ready fold 才能证明切分 | 不从样本反推生产时间轴；不使用 wall clock | REUSE EXISTING VALIDATOR |
| Legacy backtest | 无 canonical PIT identity | `backend/backtest.py` | 无 | 使用 current cached K-line、data fetcher 与 current strategy assumptions | 不能证明 R29 canonical PIT execution | 维持 legacy 行为边界 | LEGACY NON-CANONICAL |

## Evidence contract

`backend/experiment_pit_validation.py` 组合以上现有 owner，输出唯一顶层 frozen contract `PITValidationEvidence`。
九个维度分别记录 `status`、稳定 `reason_code`、`declared_identity`、`provenance_status` 和 coverage。
身份声明和来源证明分开保存；有效 SHA 本身不能把维度改成 `proven`。

READY 要求所有 required dimensions 均 proven，并且至少有一个 ready walk-forward fold。整体 reason codes 稳定排序；
universe、tradability、market data、fundamental、labels 分开报告覆盖。只有 denominator 明确时才提供 ratio，未知覆盖保持 `null`。
报告投影已经包含 train、validation、OOS periods、fold windows、coverage 和 PIT warnings，供未来 read model 使用。

当前 market-data 历史归档仍缺失，所以当前真实数据下 gate 会 fail closed。历史 universe 的完整性也仍取决于合格归档 source。
Frontend consumer deferred until canonical R29 run/read model exists；没有删除 roadmap capability，也没有伪造 experiment 页面。

## 保留的 OPEN / REQUIRED

下列问题不因 R27 / R28 / R29-A 完成而自动关闭；缺少可证明证据时继续 fail closed：

- adaptive killed terminal instant
- non-intraday paper job stable attempt identity
- `adaptive_execution_evidence` metric/status semantics
- adaptive rewards historical availability
- alpha candidate stable identity
- historical mutable candidate revisions
- parameter/experiment linkage
- historical market evidence gaps
- historical cycle membership
- physical DB origin / trusted provenance
- historical universe archive completeness
- historical market-data archive/provenance
- historical tradability experiment-wide completeness
- fundamental source historical coverage

本阶段不改 `backtest.py`，不创建 runner、API、数据库迁移或 frontend capability，不新增 manager/service/facade/framework，
不做 promotion、AI scoring、联网历史回填、R29-B、R30 或 deployment。
