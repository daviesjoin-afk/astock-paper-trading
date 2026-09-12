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
| 全市场行数/唯一代码覆盖不足（`ASTOCK_FULL_MARKET_MIN_ROWS`） | `data_fetcher._full_snapshot_payload_is_complete` / `_full_market_min_rows` | `test_full_market_snapshot_contract_accepts_complete_coverage` | `test_full_market_snapshot_contract_rejects_row_or_unique_code_shortfall` | ✅ |
| 行情陈旧（stale quote） | `data_fetcher._fresh_full_snapshot_from_disk` 内容时间戳门禁 | `test_full_market_disk_cache_accepts_fresh_content_timestamp` | `test_full_market_disk_cache_rejects_stale_content_timestamp` | ✅ |
| 双源交叉容差（价格/涨跌幅） | `paper_trading._quotes`（`cross-source` `quote_validation`） | `test_quote_cross_check_accepts_values_within_tolerance` | `test_quote_cross_check_rejects_price_gap_over_tolerance`、`test_quote_cross_check_rejects_pct_gap_over_tolerance` | ✅ |
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
| cutoff 精度契约（date-only 保持日期精度、语义仍为交易所**日末**且**不**被 canonical 化成 timestamp；完整时间戳归一到精确 canonical instant 且**绝不回退到所在自然日的日末**；`Z` / `+08:00` / naive（按 UTC+08:00）/ canonical 四种写法 → 同一 `DatasetBuild.cutoff` + manifest cutoff + fingerprint + 准入行集合；不同 instant ⇒ 不同指纹；`feature_asof` 边界改走 canonical instant 比较；显式但不可解析的 cutoff → `build_dataset` 抛 `ValueError`、`contract_status` 记 `dataset_cutoff_unprovable` 且不回退到 `_latest_provable_cutoff`；**细于 canonical 秒粒度**的 cutoff 同样被拒绝，绝不四舍五入到秒 ⇒ 两个不同瞬间不会塌成同一指纹；判定基于**解析后的语义**（`_parse_datetime` 先解析，再要求解析结果自身**与**其**绝对 UTC instant** 的 `microsecond` **都** == 0——亚秒精度可藏在 wall clock 或 **UTC offset**），因此 basic ISO 亚秒写法（`20260102T100000.100000+0800`）以及挂在 **UTC offset** 上的亚秒写法（`2026-01-02T10:00:00+08:00:00.5`，其 wall clock `microsecond == 0`）都无法绕过；再加一道**窄的拼写门禁** `_ZERO_OFFSET_FRACTION`（形状为 `[+-]` 紧跟 `(?:00|00:?00|00:?00:?00)[.,]\d*[1-9]\d*$`，后缀锚定）兜住**解析器丢弃小数**的盲区（`10:00:00+00:00:00.100000` 被 `fromisoformat` 归一成整秒，wall 与绝对瞬间**都**为 0，只有拼写看得见）：它只匹配「**零** offset + **非零**小数秒」这一语法，且覆盖零 offset 的**全部三种书写语法**——小时（`±00`）、小时+分钟（`±0000` / `±00:00`）、小时+分钟+秒（`±000000` / `±00:00:00`），`.` 或 `,` 皆可，`$` 锚定在末尾 offset 上（早期只写带秒的那种，故缩写零 offset `+00.5` / `+0000.5` / `+00:00.5` 会漏）——实测只有**零** offset 丢数，非零 offset（`+08:00:00.5` 等）仍由语义校验兜；键在语法结构而非冒号，故 basic / extended 同覆盖；整秒零 offset（`Z` / `+00` / `+00:00` / `+000000` / `+00:00:00`）与**全零小数**（`+00:00:00.000000` / `+00:00.000000`）不受影响——早期只认 `:` 的正则 `_SUBSECOND` 已删除；缩写零 offset 的小数同样经公开路径验证：`normalize_cutoff` 返回 `None`、`build_dataset` 抛 `ValueError`、`contract_status` 记 `dataset_cutoff_unprovable` 且不回退、两个不同小数不会塌成同一身份；**再加第二道窄拼写门禁** `_SUB_MICROSECOND_FRACTION` 兜住**低于解析器自身分辨率**的精度 —— `datetime` 只保存微秒，**超出 6 位的小数被截断**只留前 6 位，而这 6 位恰为 0 时解析结果就是精确整秒，调用方写下的却是一个**非零的亚微秒**量（`10:00:00.0000001` 与 `10:00:00.0000009`、以及挂在**非零** offset 上的 `10:00:00+08:00:00.0000001` 都读成整秒并塌成同一身份），该门禁匹配「小数点分隔符 + **恰好 6 个 0** + 其后出现过非零数字」（`.` 或 `,` 皆可）；**任意长度**的全零小数（`.000000` / `.0000000`）仍是整秒、照常接受，而前 6 位非全零的小数仍由语义校验拒绝，故语义校验与两道门禁**互不架空**） | `learning_dataset._normalize_cutoff`、`normalize_cutoff`、`_parse_datetime`、`_utc_instant`、`_DAY_PRECISION`、`_ZERO_OFFSET_FRACTION`、`_SUB_MICROSECOND_FRACTION`、`_classify`、`build_dataset`、`contract_status` | `test_learning_dataset.py`（`CutoffPrecisionContractTests`、`CutoffSpellingIndependenceTests`） | ✅ |
| 负向变异验证（T1–T13 必须让新增守卫变红：T1 `build_dataset` 退回 `_date_text`；T2 `_normalize_cutoff` 把 timestamp 放宽成日期；T3 `_classify` 退回日期字符串比较；T4 `contract_status` 对非法 cutoff 回退到最新可证日期；T5 `_evaluate` 用 `_iso_date` 重新截断；T6 `build_evaluation` 用 `_iso_date` 读 dataset cutoff；T7 删掉 `build_evaluation` 对不可证明 dataset cutoff 的 fail-closed ⇒ 重新退化为“没有冻结边界”；T8 删掉 `_normalize_cutoff` 解析后的 microsecond 拒绝 ⇒ basic ISO 亚秒输入重新被截断到秒；T9 把 `_normalize_cutoff` 的亚秒拒绝退回**只看 wall clock**（删掉绝对 UTC instant 那一半）⇒ 挂在 UTC offset 上的亚秒输入（`+08:00:00.5`）重新被截断到秒；T10 删掉 `_normalize_cutoff` 的**窄拼写门禁** `_ZERO_OFFSET_FRACTION` ⇒ 被解析器丢弃小数的零 offset（`+00:00:00.100000` / `+00:00:00.900000`）重新被当成整秒 cutoff 接受并塌成同一身份；T11 把 `_ZERO_OFFSET_FRACTION` **收窄回只认带秒的零 offset**（`00:?00:?00`，即本次放宽之前的写法）⇒ 缩写零 offset 的小数（`+00:00.100000` / `+00:00.900000`、`+00.5` / `+0000.5`）重新被当成整秒 cutoff 接受并塌成同一身份；T12 删掉**第二道窄拼写门禁** `_SUB_MICROSECOND_FRACTION` ⇒ 低于解析器分辨率的非零亚微秒小数（`10:00:00.0000001+08:00`、`10:00:00+08:00:00.0000009`）重新被当成整秒 cutoff 接受并塌成同一身份；T13 把该门禁**收窄成要求非零数字紧跟 6 个 0**（`[.,]0{6}[1-9]`）⇒ 非零数字出现得更靠后的小数（`10:00:00.0000000001+08:00`）漏过去） | — | 手工执行 T1–T13 变异脚本（源码级逐字节备份 + `finally` 还原 + sha256 校验；见 PR 描述） | ✅ |
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
| date-only cutoff 保持 PR-8 交易所**日末**语义（`cutoff = 2026-09-12` ⇒ 冻结线 `2026-09-12T23:59:59+08:00`，当日盘中预测不判 future；次日 00:00 起判 future；Z / +08:00 等值时刻结论一致；**绝不与普通 availability 的日首语义混用**） | `learning_evaluation._cutoff_instant`、`_is_future_prediction` | `test_learning_evaluation.py`（`EvaluationCutoffSemanticsTests`、`NormalizationAgreementTests`） | ✅ |
| 评估层不放宽 cutoff 精度（评估路径用数据集层的 `normalize_cutoff` 按原精度重建数据集，`dataset_fingerprint` 与数据层门禁完全一致、且确实**不同于**日精度 cutoff 的结果；`build_evaluation` 把 `dataset.cutoff` 读成精确 instant，使 cutoff 之后、标签可用之前生成的预测仍判 `future_prediction`；两个契约门禁对同一 cutoff 给出同一指纹；`nonsense` / 空串一致的 fail-closed） | `learning_evaluation._evaluate`、`build_evaluation`、`_is_future_prediction` | `test_learning_evaluation.py`（`EvaluationCutoffSemanticsTests`） | ✅ |
| 公开评估器不得吞掉 cutoff 归一化失败（`dataset.cutoff` **缺失** / 不可解析 / 细于秒粒度 ⇒ 数据集层判为不可证明，`build_evaluation` 必须 fail closed：记 `evaluation_dataset_cutoff_unprovable` + `evaluation_dataset_contract_failed`，**绝不**退化成 `cutoff=None`（“无冻结边界”）；合法写法则不得产生该 blocker；且同一 instant 的可表示写法确实会拒掉那些行 ⇒ 非恒真） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`EvaluationDatasetCutoffPolicyTests`） | ✅ |
| 100% 覆盖、不许挑样本（`expected_test_keys == observed_test_keys`、`coverage_ratio == 1.0`；缺口 → `evaluation_missing_predictions`，规范集合之外 → `evaluation_unexpected_predictions`） | `learning_evaluation._holdout_key`、`build_evaluation` | `test_learning_evaluation.py`（`CoverageTests`） | ✅ |
| 可证明的 train/test 分离（`trained_through < test 起点` → 否则 `evaluation_training_overlaps_test`；`selection_partition` 只允许 `validation` → 否则 `evaluation_test_used_for_selection` / `evaluation_selection_partition_unproven`；溯源缺失 → `evaluation_training_boundary_unproven`；模型溯源只追加、重放幂等、同模型两份不同溯源 → `evaluation_model_ambiguous`） | `learning_evaluation.normalize_model_provenance`、`record_model_provenance`、`read_model_provenance`、`build_evaluation` | `test_learning_evaluation.py`（`ModelProvenanceTests`） | ✅ |
| 产物归属（预测自带 `model_artifact_fingerprint` 必须与已声明溯源一致，否则 `unattributed_model_artifact` 并拒绝） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`PredictionIdentityV2Tests`） | ✅ |
| 评估指纹绑定真实产物（`model_version` / `model_artifact_fingerprint` / `training_dataset_fingerprint` / `trained_through` / `selection_partition` / `hyperparameters_fingerprint` / `random_seed` 任一变化即改变指纹） | `learning_evaluation.evaluation_fingerprint` | `test_learning_evaluation.py`（`ModelArtifactFingerprintTests`） | ✅ |
| 时间序列尾部稳健性（tail 先由 **canonical held-out 日期集合**切出、再逐日检查；只取最近 `max(MIN_HOLDOUT_DATES, ceil(HOLDOUT_FRACTION×canonical 日数))` 日；不足 → `evaluation_holdout_insufficient`；均值非正 → `evaluation_holdout_mean_not_positive`；退化（保留率 < `HOLDOUT_RETENTION_FLOOR` 或正 IC 占比 < `MIN_HOLDOUT_POSITIVE_RATIO`）→ `evaluation_holdout_deterioration`） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`TailRobustnessTests`） | ✅ |
| canonical tail 不允许跳过退化最近日期（真正的最近 3 个 test 日预测全常数 / 标签全常数 ⇒ `evaluation_undefined_ic_dates` + `evaluation_holdout_incomplete`，`contract_ok=false`；`holdout_start_date` 始终等于 canonical 尾部真实起点，不向过去漂移；全窗审计 `canonical_test_date_count` / `valid_ic_date_count` / `undefined_ic_date_count` / `undefined_ic_dates` 与尾部 `holdout_valid/undefined_*` 一并入库与入指纹） | `learning_evaluation.build_evaluation`、`evaluation_fingerprint`、`persist_evaluation_manifest` | `test_learning_evaluation.py`（`CanonicalTailTests`、`SensitivityTests`、`ManifestTests`） | ✅ |
| 逐日截面 Spearman rank IC（平均秩处理并列；单调变换不变；退化截面 undefined 不记 0） | `learning_evaluation.spearman_rank_ic`、`_average_ranks` | `test_learning_evaluation.py`（`RankIcTests`） | ✅ |
| 同日非独立（置信区间以**日期**为单位，n=有效日数；同日加标的不会缩小标准误） | `learning_evaluation._mean_and_bound` | `test_learning_evaluation.py`（`SameDateDependenceTests`） | ✅ |
| 置信下界 + holdout 门禁（有效日数下限、均值下限、下界必须为正；单日无区间） | `learning_evaluation.build_evaluation` | `test_learning_evaluation.py`（`ConfidenceGateTests`） | ✅ |
| 缺失/非有限分数不插补（NaN / None / 非数字 → `invalid_prediction_score`，绝不记 0） | `learning_evaluation._canon_number`、`_finite` | `test_learning_evaluation.py`（`PredictionIdentityTests`） | ✅ |
| 有界读取截断边界（`LIMIT max_prediction_rows + 1`，恰好等于上限不算截断；truncated 显式传入契约） | `learning_evaluation.read_prediction_evidence`、`contract_status` | `test_learning_evaluation.py`（`ConfidenceGateTests`） | ✅ |
| 评估 manifest 内容寻址 + 只追加（同证据同参数 ⇒ 同 SHA-256；`created_at` 不入指纹；`prediction_digest` 绑定所判证据） | `learning_evaluation.evaluation_fingerprint`、`persist_evaluation_manifest` | `test_learning_evaluation.py`（`ManifestTests`、`DeterminismTests`） | ✅ |
| 评估指纹敏感度（分数 / 模型 / held-out 分区 / 评分下限 / 尾部参数 / 覆盖率 / 数据集绑定 / 截断 / **canonical test 与 tail 的 undefined 日期身份**） | `learning_evaluation.evaluation_fingerprint` | `test_learning_evaluation.py`（`SensitivityTests`） | ✅ |
| 评估 manifest 记录模型/训练溯源与覆盖/尾部指标（`model_version`、`model_artifact_fingerprint`、`training_dataset_fingerprint`、`trained_through`、`selection_partition`、`provenance_fingerprint`、`coverage_ratio`、`holdout_date_count`、`canonical_test_date_count`、`undefined_ic_dates` …）且只追加 | `learning_evaluation.persist_evaluation_manifest`、`read_evaluation_manifest` | `test_learning_evaluation.py`（`ManifestTests`、`ModelArtifactFingerprintTests`） | ✅ |
| 跨模块归一化一致（本地 `_canon_number` / `_finite` / `_iso_date` / **`_cutoff_instant`** 与数据集层不漂移） | `learning_evaluation` ↔ `learning_dataset` | `test_learning_evaluation.py`（`NormalizationAgreementTests`） | ✅ |
| 三闸合放权（`dataset_contract_ok AND evaluation_contract_ok AND human_approved`；数据就绪但无样本外证据 → `approval_waiting_evaluation`） | `neural_shadow._evaluation_gate`、`readiness`、`control_status` | `test_learning_evaluation.py`（`NeuralShadowGateTests`） | ✅ |
| 无网络 / 无训练 / 无执行（无 ML 依赖、无 `.fit` / 下单调用、只读不改库） | `learning_evaluation` | `test_learning_evaluation.py`（`SafetyTests`） | ✅ |
| 负向变异验证（N1–N7 必须让守卫变红：N1 数据集绑定、N2 held-out、N3 前瞻泄漏、N4 并列秩、N5 点估计当置信下界、N6 证据摘要、N7 预测身份；**N8–N13 必须让 v2 守卫变红：N8 放开 held-out 分区、N9 关闭覆盖门禁、N10 接受无训练溯源、N11 接受 selection=test、N12 prediction_id 不绑定 asof、N13 关闭尾部退化门禁；N14 把 cutoff 退回 availability 时钟、N15 把 tail 退回"产生过 IC 的日期"** ⇒ 合计 15/15） | — | 手工执行 N1–N7 / N8–N15 变异脚本（源码级变异逐字节备份 + `finally` 还原 + sha256 校验；见 PR 描述） | ✅ |

## 维护约定

1. 新增门禁必须先在本表加一行，再写测试；表格状态从 ⚠️ → ✅。
2. `⚠️` = 逻辑已实现但缺独立离线单测，是贡献者 `good first issue` 的优先候选。
3. demo 叙事用例（`test_demo_seed` / `test_demo_replay_golden`）作为引擎路径的端到端兜底回归。
