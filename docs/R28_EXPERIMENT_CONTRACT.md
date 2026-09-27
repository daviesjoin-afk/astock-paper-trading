# R28-A — Strategy Experiment Identity & Result Contract

## 阶段状态

| 阶段 | 状态 | 边界 |
| --- | --- | --- |
| R27 | COMPLETE | R27-B2C 与 R27-B3 均已合并完成；既有 provenance 缺口仍 OPEN / REQUIRED。 |
| R28-A | COMPLETE | 只定义不可变实验身份与统一结果结构；没有 runner、caller、API 或数据库迁移。 |
| R29 | NOT STARTED | 负责 PIT Validation Lab 与 canonical execution。 |
| R30 | NOT STARTED | 负责 robustness。 |

**R28-A 回答两个问题：什么输入构成同一个策略实验，以及实验结果至少如何表达。** 它不执行
实验，不决定策略能否上线，也不赋予 AI 或结果晋级权限。production caller 数量为 **0**，因为
R28 定义 immutable contract，R29 才负责 canonical PIT execution。

## Experiment input authority matrix

`PIT provable` 表示目前是否能从现有 owner 身份和时间语义证明该维度的历史可见性。R28 的纯契约
只校验显式输入，不替调用者证明输入来自可信 owner。身份缺口未关闭时，production 仍须 fail closed。

| DIMENSION | CURRENT SOURCE | CANONICAL OWNER | CURRENT STABLE IDENTITY | PIT PROVABLE? | MUTABLE? | CURRENT FALLBACK | R28 TREATMENT |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Strategy version | `strategy_registry.StrategyVersion` / `paper_strategy_versions` | `strategy_registry` | `strategy_id + version + SHA-256 checksum`；版本行不可变 | 版本可显式固定；省略版本时 `get_version` 会读 current head，不能用于历史实验 | head 可变；已发布版本不可变 | current head 参数默认值 | REUSE CANONICAL IDENTITY；必须显式 pin 三字段 |
| Code revision | application / CLI / future runner | caller boundary | caller 提供的 40/64 位 Git SHA | 显式 revision 可固定 | 新提交会变化 | 无；禁止读取运行时 Git HEAD | DEFINE PURE CONTRACT FIELD |
| Dataset version | `learning_dataset` / `learning_dataset_manifests` | `learning_dataset` | 内容寻址 `dataset_fingerprint`；`created_at` 不进入 fingerprint | manifest 有 cutoff 与样本可用性语义；须引用精确 fingerprint | append-only manifest | 禁止 latest manifest / cutoff 代替 identity | REUSE CANONICAL IDENTITY |
| Universe version | `universe.py` 与调用方当前名单 | 尚无完整历史 universe snapshot owner | 没有可复用的实验级稳定 fingerprint | NO；历史成员关系仍 OPEN / REQUIRED | 当前名单会变化 | 不接受 current universe / 日期 / 行数伪装版本 | OPEN / REQUIRED；必填显式 SHA-256 |
| Tradability version | `tradability_archive.TradabilityEvidence` | `tradability_archive` | 单条事实有 `evidence_fingerprint`；尚无实验级完整 snapshot identity | 单条事实带 observed/effective time；完整历史 universe coverage 未证明 | archive 可追加事实 | 不接受 latest 事实或当前可交易状态 | OPEN / REQUIRED；不把单条指纹升级为 snapshot |
| Market-data version | `market_data_contract` / `market_data_service` | R24 market-data owner | snapshot 有内容、as-of、observed-at 与 verification；没有实验级 snapshot fingerprint | 对某次显式读取可判断；不能据此证明当前缓存重建历史数据 | cache 可更新 | research 读路径不得联网回填历史 | OPEN / REQUIRED；必填显式 fingerprint，不声称 owner provenance |
| Parameter set | explicit experiment input；邻近数据在 `paper_parameter_versions` | parameter history owner/linkage 尚未闭合 | 本次完整参数快照可 canonicalize；版本到历史实验的链接仍 OPEN / REQUIRED | 参数快照本身可冻结；其历史来源未证明 | 当前配置可变；历史链接未证明 | 禁止读 current parameters 补值 | DEFINE PURE CONTRACT FIELD；完整深冻结参数对象 |
| Date range | caller input | ExperimentSpec | 显式 `start` / `end` 日期 | 只证明范围声明，不证明底层数据 PIT | immutable in spec | 无 earliest / today 默认 | DEFINE PURE CONTRACT FIELD |
| As-of policy | caller input；R27 的 `ResearchAsOfContext` 是 research 语义，不是实验执行 owner | future R29 runner boundary | `policy_id + cutoff` | 必须由未来 runner 解释并执行；当前纯契约只固定声明 | immutable in spec | 禁止运行时选择“当天最新数据” | DEFINE PURE CONTRACT FIELD；instant 转为 UTC，date-only 保留日期语义 |
| Execution assumptions | `execution_profiles` 与 paper execution rules | 分散于现有 execution owners | `execution_profile_version` 存在；完整 fill/T+1/涨跌停/部分成交/容量快照尚无一份 owner fingerprint | 版本与显式快照可以绑定；历史 rule provenance 未完全证明 | 规则版本可演进 | 禁止读取 current runtime profile | DEFINE PURE CONTRACT FIELD；保存完整显式快照 |
| Cost model | `paper_trading_rules` 与 `backtest.py` 中有独立常量 | 未形成 canonical experiment cost owner | 无统一实验级 identity | 不可由 legacy constants 证明 | 常量可修改 | 禁止隐式读取 backtest 全局常量 | DEFINE PURE CONTRACT FIELD；费率、最低佣金、印花税、滑点模型、非空滑点参数对象与版本必填，并全部进入 fingerprint |
| Random seed | caller input | 无运行时 owner | 显式整数 | 可固定声明 | immutable in spec | 禁止 timestamp / random default | DEFINE PURE CONTRACT FIELD |

### Existing evaluation and legacy surfaces

- `learning_evaluation.ExperimentEvaluationProjection` 是 owner 签发的 **evaluation fact**，含 evaluation 与
  dataset fingerprint、可用时间及 owner verification。它不是完整 strategy experiment identity，也不是
  `ExperimentResult`；不得复制其核验语义、把 `evaluation_admitted` 当 promotion，或把 `mean_rank_ic`
  当策略总收益。
- `adaptive_alpha_candidates` 使用可覆盖/删除后重写的候选行，稳定候选身份与 mutable revision 仍
  OPEN / REQUIRED；它不是 canonical experiment ledger。
- `adaptive_ab_tests` 是已部署参数的观测/门禁事实，不是可重放实验清单。
- `learning_dataset_manifests` 和 `learning_evaluation_manifests` 各自继续由原 owner 管理；evaluation
  manifest 不能替代完整 ExperimentSpec。
- `backtest.py` 与现有 `walk_forward_validation.py` 保留为 **LEGACY NON-CANONICAL** surfaces。R28-A
  不改写 backtest，不将其输出升级成 canonical result，也不迁移 walk-forward / OOS / embargo 语义。

## Canonical contract

`backend/experiment_contract.py` 仅依赖 Python 标准库，包含：

- `StrategyIdentity`：显式 strategy id、版本号（>=1）与 64 位 SHA-256 checksum。
- `ExperimentSpec`：明确固定 strategy identity、code revision、dataset/universe/tradability/market-data
  fingerprint、完整参数、显式日期范围、as-of policy、execution assumptions、cost model 与整数 seed。
- `ExperimentResult`：用 `completed / failed / unavailable` 闭集表达结果；统一 return、drawdown、volatility、
  turnover、trade count、cost、exposure、capacity proxy、regime breakdown、data coverage 和 failure reason。

嵌套 JSON-like 数据会深冻结。规范 JSON 使用 `sort_keys=True`、紧凑 separators、UTF-8 与 `allow_nan=False`，
之后用 SHA-256 生成 fingerprint。`experiment_id` 等于 spec fingerprint；任何显式 material input 改变都会
改变 identity。Result 含其 exact experiment fingerprint，并用另一份 SHA-256 覆盖身份、status、全部指标与
failure reason。

缺少身份、current/latest 占位、缺失滑点参数、缺失日期、反向日期、非有限数值、错误结果绑定都会拒绝。
带时区的 as-of instant 统一转换为 UTC `+00:00`；date-only cutoff 仍表示日期，不转换成 midnight instant。completed 要求
核心指标齐全；failed / unavailable 要求稳定 reason 且不允许填造指标。unknown 保持 `null`，不折算为零。
capacity proxy、exposure 与 regime breakdown 可以保持 unavailable；本阶段不发明评分。

契约不含 approved/promotable/champion/production 状态，也不读取数据库、网络、文件、Git、时钟、LLM 或
全局配置。不新增 service、manager、facade、framework、API、生产 caller 或 DB migration。当前 R28-A 的
business authority 仅为 **experiment identity/result contract**。

## R27 缺口状态

以下仍 **OPEN / REQUIRED**，不随 R27 COMPLETE 或本契约创建而关闭；缺少可证明输入时 production 必须
fail closed：

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

## Exit and verification

R28-A 的 exit 只覆盖纯契约：12 个 material dimensions 显式固定；稳定 identity 与 result fingerprint；深冻结；
strict finite metrics；unknown 不变零；结果精确绑定；无 promotion 语义；无 I/O；无 DB migration；复用现有
evaluation owner fact；backtest 保持 non-canonical。测试与 mutation 的详细结果记录在 PR body 和 exact-head CI。
