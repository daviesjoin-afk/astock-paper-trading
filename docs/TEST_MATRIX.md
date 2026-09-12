# 撮合、策略平台与前端门禁场景矩阵

对应 issue #27 / #23：一眼看出每个关键交易门禁的覆盖与缺口。
约定：**正向** = 允许成交的用例；**拒绝** = 必须拦下的用例。全部测试离线可跑；
浏览器 E2E（`frontend/e2e/`）由 CI 的 `browser-e2e (chromium)` 作业执行，同样使用合成数据。

## 交易规则层（`paper_trading_rules.py`）

| 场景 | 实现位置 | 正向用例 | 拒绝用例 | 状态 |
| --- | --- | --- | --- | --- |
| 证券权限（主板/创业板） | `security_scope` | `test_security_scope_keeps_risk_boards_closed`（正向分支） | 同用例（科创板/北交所/ST 拒绝） | ✅ |
| T+1 vs ETF T+0 | `asset_type` | `test_asset_type_distinguishes_etf_t0_from_stock_t1` | 同用例 | ✅ |
| 佣金（万分之 0.1、无最低） | `commission` | `test_commission_uses_current_no_minimum_policy` | — | ✅ |
| 涨跌停幅度（主板 10% / 创业板·科创板 20% / ST 5%） | `limit_pct` + `factors.limit_up_threshold` | 本矩阵新增 rules 测试 | — | ✅（幅度档位）；拒单联动见撮合层 |
| 印花税（卖出 0.05%） | 成交流径 `fees = commission + amount*STAMP_SELL`（卖单向） | demo 账本断言含费用字段 | — | ⚠️ 引擎级集成覆盖，无独立单测 |
| 滑点（±0.1%） | 成交流径 `price * (1 ± SLIPPAGE)` | demo 账本成交价叙事 | — | ⚠️ 引擎级集成覆盖，无独立单测 |
| 整手（100 股倍数） | `paper_sizing.floor_to_100` / `paper_allocation` | `test_paper_sizing.py`（6 用例含动态最小下单额） | — | ✅（-sizing 层）；撮合层整手拒绝待补 |
| 交易 日（T+1 解锁日） | `next_weekday` → `universe.next_trade_day` | demo 叙事 + 撮合端 T+1 拒单测试 | demo：当日买→当日卖被拒（`test_demo_seed` / golden replay） | ✅ |

## 数据质量层（issue #23）

| 场景 | 实现位置 | 正向用例 | 拒绝用例 | 状态 |
| --- | --- | --- | --- | --- |
| 快照页缺失 → fail-closed | `data_fetcher.fetch_market_snapshot` / `_fetch_clist` | `test_full_market_snapshot_fails_closed_on_partial_pages`（正向=完整时通过） | 同用例（partial → `[]`） | ✅ |
| 全市场行数不足（`ASTOCK_FULL_MARKET_MIN_ROWS`） | `data_fetcher._full_market_min_rows` + 快照完整性门禁 | demo 模式阈值=1（`test_demo_seed`） | 部分页 → `complete=False`（同上） | ✅ |
| 行情陈旧（stale quote） | 撮合端：`quote_at` 新鲜度门禁（`paper_trading` data_quality/`_market_state`） | demo：正常价成交 | demo：陈旧报价拒单（`test_demo_seed` 叙事 + golden replay 锁定） | ✅（撮合端）；数据层独立用例待补 |
| 双源交叉容差（价格/成交量） | `data_fetcher` 交叉核验（`cross-source` `quote_validation`） | demo 叙事正常成交 | — | ⚠️ 状态位已落 audit；独立容差单测待补 |
| 来源健康状态/熔断 | `marketdata_transport`（timeout/retries/腾讯熔断）+ `data_source_health.json` + `/api` 暴露 | `test_source_health_round_trip`、`test_missing_source_health_is_empty` | 熔断打开时回退备用源（引擎路径） | ✅（状态与暴露）；熔断行为单测待补 |

## 撮合执行层

| 场景 | 实现位置 | 正向用例 | 拒绝用例 | 状态 |
| --- | --- | --- | --- | --- |
| 手动下单两段确认 | `manual_orders.submit_manual_order`（`confirmed=true` 才执行） | demo 叙事成交 | `api_paper` 400（无 confirmed） | ✅ |
| 硬止损卖出 | `paper_trading` 风控卖出路径 | demo：56.40 止损成交叙事 | — | ✅ |
| 涨停买入 | 撮合成交假设 `snapshot_price_rule` | demo：600906 涨停成交叙事 | — | ✅ |
| 资金容量 / 共享池席位 | `paper_allocation`、`_shared_cash`、席位预算 | `test_paper_sizing`（现金约束/敞口缩放） | — | ⚠️ sizing 层 ✅；撮合层并发拒绝用例待补 |
| 停牌标的 | universe `snapshot_tradable` / `risk_flag` | — | 选股层排除（universe 构建侧） | ⚠️ 撮合端独立用例待补 |

## 策略平台层

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 版本不可变（编辑追加新版本，旧版本与 checksum 不变） | `strategy_registry.save_definition` | `test_strategy_version_immutability.py`、`test_strategy_versioning.py` | ✅ |
| 乐观并发（`expected_version` 不匹配即冲突，不静默覆盖） | `strategy_registry` / `strategy_service` | `test_strategy_version_immutability.py`、`test_api_strategies.py` | ✅ |
| 运行时就绪（DSL / 指纹 / 风险画像 / 执行画像四项编译） | `strategy_registry.runtime_readiness` | `test_strategy_crud.py`、`test_api_strategies.py::test_transition_validated_requires_runtime_ready` | ✅ |
| 生命周期合法边与 `supports_new_cycle` 派生 | `strategy_registry._TRANSITIONS` | `test_strategy_registry_lifecycle.py`、`test_strategy_invariant_matrix.py` | ✅ |
| 草稿硬删除边界（从未离开 draft ∧ 无历史引用） | `strategy_registry.hard_delete_unused_draft` | `test_strategy_hard_delete_lifecycle.py`、`test_strategy_crud.py` | ✅ |
| 归档而非删除（archived 保留版本与审计、可回放） | `strategy_registry`、`paper_archive_projection` | `test_strategy_archive_replay.py` | ✅ |
| Strategy API 契约（路由/请求体/状态码/错误提示） | `api_strategies.py`、`strategy_api_models.py` | `test_strategy_api_contract.py`、`test_api_strategies.py` | ✅ |
| Strategy Service（用例编排、异常翻译、无库路径） | `strategy_service.py` | `test_strategy_service.py`、`test_strategy_single_editor.py` | ✅ |
| DSL 校验与求值（白名单、fail-closed、参数节点） | `strategy_dsl_schema.py`、`strategy_dsl_evaluator.py` | `test_strategy_dsl.py`、`test_strategy_invariants.py`、`test_strategy_parameter_schema.py` | ✅ |
| 数量越权拒绝（策略声明 qty/shares/amount 即拒） | `order_intent.reject_qty_claims` | `test_order_intent_contract.py`、`test_order_intent.py` | ✅ |
| 动态分配性质（Σ 分配 ≤ 池上限、N 策略等价与单策略退化） | `paper_allocation.py` | `test_paper_allocation.py`、`test_strategy_properties.py` | ✅ |
| 周期所有权 ≠ 执行资格（pause 保留资金、resume 不放大） | `paper_trading.current_cycle_participant_ids` | `test_cycle_ledger_ownership.py`、`test_cycle_participant_resolver.py` | ✅ |
| 零策略 Idle 周期（显式空集合合法，不回落全集） | `runtime_settings`、`paper_trading._cycle_participant_resolution` | `test_settings_registry_driven.py`、`test_runtime_settings.py` | ✅ |
| 生产路径 golden replay（策略创建→激活→周期→信号→订单→成交→净值） | — | `test_production_path_golden_replay.py` | ✅ |
| 策略产品线端到端（内置模板 + 用户策略同链路） | — | `test_strategy_product_line_e2e.py` | ✅ |
| 风险收紧（画像只能更严，系统键不可触碰） | `strategy_risk_enforcement.py` | `test_strategy_risk_enforcement.py`、`test_asymmetric_risk_gate_wiring.py` | ✅ |

## 前端与构建层

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 前端模块契约（inline handler 全部桥接、跨模块必须 import） | `frontend/src/bridge.js`、各模块 | `test_frontend_module_contract.py` | ✅ |
| 构建身份一致（入口 build id == `backend/build_info.py` == dist） | `frontend/src/app.js`、`backend/build_info.py` | `test_frontend_module_contract.py::test_dist_bundle_is_built_from_the_new_sources` | ✅ |
| 样式拆分保序（index.css 片段齐全、行区间连续） | `frontend/styles/**` | `test_frontend_module_contract.py::test_styles_entry_imports_every_style_file_in_order` | ✅ |
| 浏览器 E2E（工坊关键旅程：创建/生命周期/设置/版本/克隆/深链接/响应式） | `frontend/e2e/**` | `frontend/e2e/specs/journey1-create-draft.spec.js`、`strategy-*.spec.js`、`paper-runtime.spec.js`、`responsive-accessibility.spec.js`、`bridge-contract.spec.js`（CI `browser-e2e (chromium)`） | ✅ |
| 设置中心 Registry 驱动（不出现固定"五套"表述、勾选资格门禁） | `frontend/src/features/settings.js` | `test_settings_registry_driven.py` | ✅ |
| Docker 冒烟（`--network none` + tmpfs 跑全量离线测试层 + 健康端点） | `Dockerfile`、`docker-compose*.yml` | CI `docker-smoke` 作业 | ✅ |
| 仓库卫生与文档链接（无断链、无废弃产物引用、无未声明依赖） | `docs/**`、根 markdown | `test_repository_hygiene.py` | ✅ |

## 学习数据集契约（PR-8）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| PIT 可用性（`feature_available_at <= cutoff`，未知/未证明一律排除） | `learning_dataset._classify`、`financial_point_in_time` | `test_learning_dataset.py`（`PitContractTests`） | ✅ |
| PIT 时区契约（canonical UTC instant；date-only cutoff = 中国市场自然日结束 `T15:59:59Z`；naive 按 UTC+08:00；不依赖机器 local tz） | `learning_dataset._timestamp_text`、`_instant`、`_cutoff_instant`、`_canonical_instant` | `test_learning_dataset.py`（`TimezoneContractTests`） | ✅ |
| Canonical provenance 保留字段（raw evidence provenance 不得覆盖 `availability_clock` / `sample_contract_version` / `label_source` / `label_source_version` / `industry` / `regime`；非保留审计字段保留） | `learning_dataset._canonical_provenance`、`CANONICAL_RESERVED_PROVENANCE_KEYS` | `test_learning_dataset.py`（`CanonicalProvenanceTests`） | ✅ |
| 标签成熟度与 cutoff（`feature_asof < label_end_date <= cutoff`；cutoff 之后成熟/可用判 `future_label`，不 clamp/不回填） | `learning_dataset._classify` | `test_learning_dataset.py`（`LabelContractTests`） | ✅ |
| 标签 PIT 独立 fail-closed（`label pit_status = verified` 才准入；时间戳存在 ≠ provenance 已证明；未证明终点不得继承为 verified） | `learning_dataset._classify`、`adaptive_engine._mature_alpha_returns` | `test_learning_dataset.py`（`LabelContractTests`、`LabelPitInheritanceTests`） | ✅ |
| 冲突 label fail-closed（同一逻辑身份多 endpoint 全部排除，与插入顺序无关） | `learning_dataset._ambiguous_identities` | `test_learning_dataset.py`（`AmbiguousLabelTests`、`ExclusionAuditTests`） | ✅ |
| 时间序切分与跨界标签 purge（train→validation、validation→test） | `learning_dataset.chronological_split` | `test_learning_dataset.py`（`SplitPurgeTests`） | ✅ |
| 确定性指纹（同数据同 cutoff ⇒ 同 SHA-256；插入顺序无关） | `learning_dataset.dataset_fingerprint` | `test_learning_dataset.py`（`DeterminismTests`） | ✅ |
| 指纹敏感度（特征/可用性/目标/标签终点/特征表/cutoff/切分/契约版本） | `learning_dataset.dataset_fingerprint` | `test_learning_dataset.py`（`SensitivityTests`） | ✅ |
| 缺失/未知不插补（None / NaN / Infinity 不允许变成 0） | `learning_dataset._classify`、`_finite` | `test_learning_dataset.py`（`MissingDataTests`） | ✅ |
| 排除审计（每次构建输出 `exclusion_reasons`，不静默丢弃） | `learning_dataset.build_manifest` | `test_learning_dataset.py`（`ExclusionAuditTests`） | ✅ |
| 幂等迁移（旧库/空库/测试库；legacy 行标 `legacy_unproven` 不伪造） | `learning_dataset.ensure_schema` | `test_learning_dataset.py`（`SchemaMigrationTests`） | ✅ |
| 研究就绪 ≠ 行数达标（数据契约门禁，fail-closed） | `neural_shadow._dataset_gate`、`learning_dataset.contract_status` | `test_learning_dataset.py`（`ContractStatusTests`） | ✅ |
| 有界读取截断边界（`LIMIT max_evidence_rows + 1`，恰好等于上限不算截断；truncated 为显式字段） | `learning_dataset._read_alpha_evidence_page`、`contract_status` | `test_learning_dataset.py`（`ContractStatusTests`） | ✅ |
| 无网络 / 无执行（只消费已持久化证据，不触碰下单与风控） | `learning_dataset` | `test_learning_dataset.py`（`NoNetworkTests`、`NoExecutionTests`） | ✅ |
| 负向变异验证（N1–N11 必须让守卫变红；含 N7 future label、N8 ambiguous label、N9 label PIT、N10 timezone cutoff、N11 provenance override） | — | 手工执行 N1–N11 变异脚本（见 PR 描述） | ✅ |

## 学习评估契约（PR-9）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 预测证据内容寻址（`prediction_id = sha256(canonical material evidence)`：contract/schema 版本、数据集、模型、**模型产物**、样本、代码、分区、标签起始日、分数、**生成时刻**、来源，与行位置无关） | `learning_evaluation.prediction_identity`、`normalize_prediction` | `test_learning_evaluation.py`（`PredictionIdentityTests`、`PredictionIdentityV2Tests`） | ✅ |
| 预测证据只追加不可变（主键 + `INSERT OR IGNORE`，重放幂等、不改写） | `learning_evaluation.record_predictions`、`read_prediction_evidence` | `test_learning_evaluation.py`（`PredictionIdentityTests`） | ✅ |
| 冲突分数 fail-closed（同一身份两个不同分数全部排除，与插入顺序无关） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`PredictionIdentityTests`） | ✅ |
| 数据集指纹绑定（跨数据集预测一律 `unbound_prediction`；缺失 fingerprint 关闭门禁） | `learning_evaluation._binds_dataset` | `test_learning_evaluation.py`（`FingerprintBindingTests`） | ✅ |
| 模型归属（多模型且未显式指定 ⇒ `evaluation_model_ambiguous`；他模型行记 `other_model` 不参与打分） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`FingerprintBindingTests`） | ✅ |
| 只有 `test` 可被评估（`SUPPORTED_HOLDOUT_PARTITIONS` 闭集；train/validation/未知名 → `evaluation_holdout_partition_unsupported`，即便 IC 完美也不放行） | `learning_evaluation.SUPPORTED_HOLDOUT_PARTITIONS`、`build_evaluation` | `test_learning_evaluation.py`（`HoldoutPartitionTests`） | ✅ |
| 时序 held-out（分区不符 → `partition_mismatch`；非 held-out → `not_held_out`） | `learning_evaluation._is_held_out` | `test_learning_evaluation.py`（`HoldoutLeakageTests`） | ✅ |
| 前瞻泄漏（`prediction_asof > cutoff` → `future_prediction`） | `learning_evaluation._is_future_prediction` | `test_learning_evaluation.py`（`HoldoutLeakageTests`） | ✅ |
| 预测可用时刻 fail-closed（缺失/不可解析 → `unproven_prediction_availability`，不可参与且造成覆盖缺口；必须证明 `prediction_asof < label_available_at`，同 PR-8 交易所时钟，naive 按 UTC+8） | `learning_evaluation._availability_instant`、`_unproven_availability`、`_is_future_prediction` | `test_learning_evaluation.py`（`PredictionAvailabilityTests`） | ✅ |
| 100% 覆盖、不许挑样本（`expected_test_keys == observed_test_keys`、`coverage_ratio == 1.0`；缺口 → `evaluation_missing_predictions`，规范集合之外 → `evaluation_unexpected_predictions`） | `learning_evaluation._holdout_key`、`build_evaluation` | `test_learning_evaluation.py`（`CoverageTests`） | ✅ |
| 可证明的 train/test 分离（`trained_through < test 起点` → 否则 `evaluation_training_overlaps_test`；`selection_partition` 只允许 `validation` → 否则 `evaluation_test_used_for_selection` / `evaluation_selection_partition_unproven`；溯源缺失 → `evaluation_training_boundary_unproven`；模型溯源只追加、重放幂等、同模型两份不同溯源 → `evaluation_model_ambiguous`） | `learning_evaluation.normalize_model_provenance`、`record_model_provenance`、`read_model_provenance`、`build_evaluation` | `test_learning_evaluation.py`（`ModelProvenanceTests`） | ✅ |
| 产物归属（预测自带 `model_artifact_fingerprint` 必须与已声明溯源一致，否则 `unattributed_model_artifact` 并拒绝） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`PredictionIdentityV2Tests`） | ✅ |
| 评估指纹绑定真实产物（`model_version` / `model_artifact_fingerprint` / `training_dataset_fingerprint` / `trained_through` / `selection_partition` / `hyperparameters_fingerprint` / `random_seed` 任一变化即改变指纹） | `learning_evaluation.evaluation_fingerprint` | `test_learning_evaluation.py`（`ModelArtifactFingerprintTests`） | ✅ |
| 时间序列尾部稳健性（只取最近 `max(MIN_HOLDOUT_DATES, ceil(HOLDOUT_FRACTION×有效日数))` 日；不足 → `evaluation_holdout_insufficient`；均值非正 → `evaluation_holdout_mean_not_positive`；退化（保留率 < `HOLDOUT_RETENTION_FLOOR` 或正 IC 占比 < `MIN_HOLDOUT_POSITIVE_RATIO`）→ `evaluation_holdout_deterioration`） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`TailRobustnessTests`） | ✅ |
| 逐日截面 Spearman rank IC（平均秩处理并列；单调变换不变；退化截面 undefined 不记 0） | `learning_evaluation.spearman_rank_ic`、`_average_ranks` | `test_learning_evaluation.py`（`RankIcTests`） | ✅ |
| 同日非独立（置信区间以**日期**为单位，n=有效日数；同日加标的不会缩小标准误） | `learning_evaluation._mean_and_bound` | `test_learning_evaluation.py`（`SameDateDependenceTests`） | ✅ |
| 置信下界 + holdout 门禁（有效日数下限、均值下限、下界必须为正；单日无区间） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`ConfidenceGateTests`） | ✅ |
| 缺失/非有限分数不插补（NaN / None / 非数字 → `invalid_prediction_score`，绝不记 0） | `learning_evaluation._canon_number`、`_finite` | `test_learning_evaluation.py`（`PredictionIdentityTests`） | ✅ |
| 有界读取截断边界（`LIMIT max_prediction_rows + 1`，恰好等于上限不算截断；truncated 显式传入契约） | `learning_evaluation.read_prediction_evidence`、`contract_status` | `test_learning_evaluation.py`（`ConfidenceGateTests`） | ✅ |
| 评估 manifest 内容寻址 + 只追加（同证据同参数 ⇒ 同 SHA-256；`created_at` 不入指纹；`prediction_digest` 绑定所判证据） | `learning_evaluation.evaluation_fingerprint`、`persist_evaluation_manifest` | `test_learning_evaluation.py`（`ManifestTests`、`DeterminismTests`） | ✅ |
| 评估指纹敏感度（分数 / 模型 / held-out 分区 / 评分下限 / 尾部参数 / 覆盖率 / 数据集绑定 / 截断） | `learning_evaluation.evaluation_fingerprint` | `test_learning_evaluation.py`（`SensitivityTests`） | ✅ |
| 评估 manifest 记录模型/训练溯源与覆盖/尾部指标（`model_version`、`model_artifact_fingerprint`、`training_dataset_fingerprint`、`trained_through`、`selection_partition`、`provenance_fingerprint`、`coverage_ratio`、`holdout_date_count` …）且只追加 | `learning_evaluation.persist_evaluation_manifest`、`read_evaluation_manifest` | `test_learning_evaluation.py`（`ManifestTests`、`ModelArtifactFingerprintTests`） | ✅ |
| 跨模块归一化一致（本地 `_canon_number` / `_finite` / `_iso_date` 与数据集层不漂移） | `learning_evaluation` ↔ `learning_dataset` | `test_learning_evaluation.py`（`NormalizationAgreementTests`） | ✅ |
| 三闸合放权（`dataset_contract_ok AND evaluation_contract_ok AND human_approved`；数据就绪但无样本外证据 → `approval_waiting_evaluation`） | `neural_shadow._evaluation_gate`、`readiness`、`control_status` | `test_learning_evaluation.py`（`NeuralShadowGateTests`） | ✅ |
| 无网络 / 无训练 / 无执行（无 ML 依赖、无 `.fit` / 下单调用、只读不改库） | `learning_evaluation` | `test_learning_evaluation.py`（`SafetyTests`） | ✅ |
| 负向变异验证（N1–N7 必须让守卫变红：N1 数据集绑定、N2 held-out、N3 前瞻泄漏、N4 并列秩、N5 点估计当置信下界、N6 证据摘要、N7 预测身份；**N8–N13 必须让 v2 守卫变红：N8 放开 held-out 分区、N9 关闭覆盖门禁、N10 接受无训练溯源、N11 接受 selection=test、N12 prediction_id 不绑定 asof、N13 关闭尾部退化门禁**） | — | 手工执行 N1–N7 / N8–N13 变异脚本（见 PR 描述） | ✅ |

## 维护约定

1. 新增门禁必须先在本表加一行，再写测试；表格状态从 ⚠️ → ✅。
2. `⚠️` = 逻辑已实现但缺独立离线单测，是贡献者 `good first issue` 的优先候选。
3. demo 叙事用例（`test_demo_seed` / `test_demo_replay_golden`）作为引擎路径的端到端兜底回归。
