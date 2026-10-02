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
| exact-version 生命周期、CAS、history 与正式周期资格 | `strategy_lifecycle` | `test_strategy_lifecycle_promotion.py`（R31-01…93）、`test_strategy_registry_lifecycle.py` | ✅ |
| R29/R30 exact evidence promotion 与 proposal apply | `strategy_promotion` | `test_strategy_lifecycle_promotion.py`（R31-28…58） | ✅ |
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

## R32-A 可比较运行上下文（COMPLETE）

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| Market snapshot identity 对行/key 顺序稳定，保留重复行；saved_at 不改变事实 identity | `market_data_contract.snapshot_fingerprint` | `test_market_data_boundary.py`（R32A fingerprint tests） | ✅ |
| 行情事实、核验语义或 observed_at 改变时 fingerprint 改变；runtime policy 单独参与 context identity | `market_data_contract`、`simulation_runtime_context` | `test_market_data_boundary.py`、`test_simulation_runtime_context.py` | ✅ |
| 精确 strategy version/checksum、market、tradability、execution ruleset 与已有 risk identity 决定 context fingerprint | `simulation_runtime_context` | `test_simulation_runtime_context.py` | ✅ |
| Active decision/execution envelope 保存显式 context；缺失身份保持 unavailable，不回填历史 | `paper_decision_audit`、`execution_planner` | `test_paper_decision_audit.py`、`test_execution_planner.py` | ✅ |
| formal execution/entry adapter 与显式 state 进入相同 evaluator；explicit execution builder 不读账本 | `execution_planner` | `test_execution_planner.py`（adapter equivalence / state purity） | ✅ |
| 语义 mutation M1–M10 覆盖事实、context identity、legacy fail-closed、state isolation、risk gate 与 fee authority | — | `work/r32a_comparable_context_mutation_check.py`（10/10 detected，restore SHA256 与恢复基线通过） | ✅ |

阶段状态：R31 **COMPLETE**；R32-A **COMPLETE（PR #216 MERGED）**。

## R32-B Active 可比较证据闭环（COMPLETE）

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| Active context 只返回 `AVAILABLE + immutable context` 或 `UNAVAILABLE + stable reason code` | `simulation_runtime_context.ActiveRuntimeContextResult` | `test_simulation_runtime_context.py` | ✅ |
| 生产 execution adapter 单次读取 Tradability Archive；同一事实绑定 context 并交给执行 evaluator | `execution_planner.execution_context_from_facts` | `test_execution_planner.ExecutionStateBuilderTests.test_production_fact_capture_binds_the_single_owner_read_to_context_and_execution` | ✅ |
| 缺失 owner fact 明确 unavailable；后续出现新归档事实不升级已捕获结果 | `execution_planner`、`paper_decision_audit` | `test_execution_planner.ExecutionStateBuilderTests.test_missing_owner_fact_remains_unavailable_after_a_later_archive_fact` | ✅ |
| 决策审计保存固定 reason code，不自行补查当前状态 | `paper_decision_audit` | `test_paper_decision_audit.py` | ✅ |

阶段状态：R32-A **COMPLETE**；R32-B **COMPLETE（PR #217 MERGED）**。

## R32-C 隔离式 Shadow Runtime（COMPLETE，PR #218 MERGED）

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| Environment identity 对象键及集合顺序稳定；冻结输入与调用者嵌套对象脱离；绑定 Active context、session、decision instant、market、quote、factor、tradability 与 execution ruleset | `shadow_runtime.ComparableEnvironmentIdentity`、`FrozenShadowEnvironment` | `test_shadow_runtime.py` | ✅ |
| Active capture 的七个共享环境维度（session、decision instant、market policy、market snapshot、quote、tradability、execution ruleset identity）逐项与环境 identity 相符，任一维不同即 NOT_COMPARABLE | `shadow_runtime.FrozenShadowEnvironment` | `test_shadow_runtime.py`（C6 逐维非空真控制） | ✅ |
| tradability 只接受 owner 的 typed immutable decision；Shadow execution 不读取 archive repository | `shadow_runtime.FrozenShadowEnvironment` | `test_shadow_runtime.py`（C3、typed evidence） | ✅ |
| 同一冻结环境、exact Challenger version、reference state 与 reference capital 得到相同 append-only ShadowRun | `shadow_runtime.run_shadow` | `test_shadow_runtime.py`（C1） | ✅ |
| decision 与 quote freshness 使用显式 `decision_at`，不读取机器时钟 | `execution_planner.evaluate_entry_state` | `test_shadow_runtime.py`（C2） | ✅ |
| Shadow runner 不读 provider/archive/cache；只接受冻结 Active evidence | `shadow_runtime` | `test_shadow_runtime.py`（C3） | ✅ |
| ShadowRun repository 只写自身 append-only 表；正式 cash/positions/lots/orders/fills/reservations/risk decisions/position risk state 不变 | `shadow_run_repository`、migration v24 | `test_shadow_runtime.py`（C4） | ✅ |
| exact challenger lifecycle 必须为 `shadow`；环境不一致、continuation 未显式绑定 prior run 时 fail closed | `shadow_run_service`、`shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（C5–C7） | ✅ |
| 新 run 后按显式 ID 重读旧 run，旧 fingerprint/replay 不变；不提供 latest fallback | `shadow_run_repository.get_run` | `test_shadow_runtime.py`（C8） | ✅ |
| Entry 与 execution 共用 isolated reference cash/state；formal cash 和 formal portfolio occupancy 不进入 Challenger 运行 | `shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（C9–C10） | ✅ |
| continuation 跨 session 使已有持仓可卖并重置当日成交量；同 session 保留 T+1 与消费量状态 | `shadow_runtime._state_for_run` | `test_shadow_runtime.py`（C8） | ✅ |
| run evidence 保存本次消费的 candidate 输入（requested qty、reference price、捕获 EntryGateState 全字段、risk identity）；重复 `(symbol, side)` candidate 拒绝 | `shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（evidence inputs、duplicate legs） | ✅ |
| run-level evidence 保存本次消费的 exact Entry Policy identity（owner projection + fingerprint + 绑定身份 + 策略行 id），可按 run ID 从 repository 逐字节读回 | `execution_planner.ExecutionPolicySnapshot`、`shadow_runtime.evaluate_shadow`、`shadow_run_repository` | `test_shadow_runtime.py`（frozen entry policy evidence、DB 往返） | ✅ |
| 显式冻结策略后，owner 策略表改成另一份策略、且 `policy_for` 直接抛错，replay 的 fingerprint 与 entry 结果仍逐字节不变 | `shadow_run_service.run_shadow`、`shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（replay invariant、服务端捕获一次） | ✅ |
| 市场灯暂停文案来自本次冻结的策略，而不是当前策略表 | `execution_planner.market_gate`、`execution_planner.evaluate_entry_state` | `test_shadow_runtime.py`（frozen market reason）、`test_execution_planner.py`（declared policy） | ✅ |
| 显式 policy 与 runtime context fingerprint 不一致时 fail closed，不回退当前策略表自动纠正；绑定到别的 account 的策略被拒 | `execution_planner.evaluate_entry_state`、`execution_planner.ExecutionPolicySnapshot` | `test_execution_planner.py`（frozen policy mismatch、forged binding） | ✅ |
| 手改 `context_fingerprint` 的伪造 context 被拒（重建校验不是恒真） | `shadow_runtime._rebuild_runtime_context` | `test_shadow_runtime.py`（forged context） | ✅ |
| 挑战者捕获的 `position_limit` 仍然生效；Shadow 只隔离正式占位/共享池预留 | `shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（capacity binding） | ✅ |
| 决策路径不打开数据库；provider/archive 读取为 0 | `shadow_runtime.evaluate_shadow` | `test_shadow_runtime.py`（C3、no-database） | ✅ |
| ShadowRun DDL 单一事实来源；migration v24 与 `init_db` 两条路径调用同一函数，既有账本升级后表与 append-only guard 齐备 | `paper_schema_migrations.ensure_shadow_runs_table`、`paper_trading.init_db` | `test_shadow_runtime.py`（v24 DDL owner、existing ledger init_db） | ✅ |
| 语义 mutation（canonical order、provider、环境、账本、prior run、lifecycle、wall clock、reference cash、T+1 rollover、typed evidence、共享环境维度比较、candidate 唯一性、证据输入、frozen entry policy） | `work/r32c_shadow_runtime_mutation_check.py` | 16/16 DETECTED，restore SHA256 PASS | ✅ |

阶段状态：R32-A/B/C/D **COMPLETE**（R32-C = PR #218 MERGED，R32-D = PR #219 MERGED）；R32-E1 **IN REVIEW**；R32-E2～E4、R33 **NOT STARTED**。

## R32-E1 Comparable Evidence Provenance Closure（IN REVIEW）

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| Active 与 Challenger 的 risk policy identity 来自同一个 owner 投影形状（唯一编译点 + 唯一字段集），Challenger 侧标 `OWNER_ISSUED` 并带 exact strategy binding | `strategy_runtime.compile_risk_policy` / `risk_policy_projection*`、`shadow_run_service` | `test_shadow_comparison.py`（E1-1）、`test_shadow_runtime.py`（frozen entry policy 读回） | ✅ |
| 风险决策有精确的 order 关联（`paper_risk_decisions.order_id`，migration v26，幂等且绝不回填）**且**带 write-time authority provenance（`payload.decision_provenance`，order-linked row 必须声明 authority，否则写入期拒绝）；只有 `RISK` authority 的 row 才进 risk_rejection，`EXECUTION` row 只进 execution 维度 | `paper_schema_migrations.ensure_risk_decision_order_linkage`、`paper_trading._risk_log`、`shadow_comparison_service._risk_decision_evidence`、`shadow_comparison._risk_dimension` | `test_shadow_comparison.py`（E1-2、E1-R1、E1-R2、E1-R3、E1-R5、E1-R10、v26 DDL owner、existing ledger init_db） | ✅ |
| 同一条 decision 文案按 authority label 分流（禁止按表名/decision 字符串/reason/status/order_id 非空分类）；未声明 provenance 的 row 永不被当作风险证据 | `shadow_comparison._decision_authority` | `test_shadow_comparison.py`（E1-R3、E1-R4） | ✅ |
| supplied risk_policy 必须 canonical-equal 于 owner 基于 exact `strategy_version.definition` 的投影（只比 key 集合不够）；篡改 fingerprint 或 profile 都 fail closed | `strategy_runtime.risk_policy_projection_for_definition`、`shadow_runtime.evaluate_shadow` | `test_shadow_comparison.py`（E1-R6、E1-R7、E1-R9） | ✅ |
| current strategy/risk config 改变但显式输入不变 → replay 报告逐字节不变（只验证显式不可变输入，不读 DB/current/latest） | `shadow_runtime.evaluate_shadow` | `test_shadow_comparison.py`（E1-R8） | ✅ |
| 属于别的 order 的风险证据被拒（fail closed），并拒绝缺 order_id 的伪证据 | `shadow_comparison.ActiveOrderEvidence` | `test_shadow_comparison.py`（E1-R4） | ✅ |
| comparison 重放不读取 current risk policy（owner 编译器被 patch 成抛错仍能完成），run 里记录的就是决策消费的那一份 | `shadow_comparison.build_shadow_comparison`、`shadow_run_service` | `test_shadow_comparison.py`（E1-4） | ✅ |
| legacy order（无 order 关联）与 legacy run（caller 自述 identity）保持 PARTIAL / UNAVAILABLE + `DECLARED`，绝不 backfill、绝不用 (account, code, side) 或"最新一条"借用 | `shadow_comparison._risk_dimension` | `test_shadow_comparison.py`（E1-5/6、D8b） | ✅ |
| order lifecycle status/reason 永不作为风险证据（只作为独立 lifecycle 事实，并标注 `used_as_risk_evidence = false`） | `shadow_comparison._order_lifecycle` | `test_shadow_comparison.py`（E1-7、D17） | ✅ |
| 旧的 caller-declared risk identity 输入与内联 projection 组装已删除（caller = 0）；owner risk policy shape 校验强制外部输入 | `strategy_runtime.is_risk_policy_projection`、`shadow_runtime.evaluate_shadow` | `test_shadow_comparison.py`（E1-8） | ✅ |
| 执行经济学（滑点/费用/参与率/手数/T+1）未被本 PR 触碰 | `execution_planner`、`paper_trading_rules` | `test_shadow_comparison.py`（E1-9） | ✅ |
| **真实 Active BUY production path**：驱动真实 `PT._buy_order`（owner 边界注入 owner 输出，不手工 INSERT 决策行）后，复合 admission 结论与 Risk Authority 否决各自成行、都精确绑定刚创建的 order_id；只有 RISK 行进 risk_rejection，容量/时机/派发拒绝绝不冒充 RISK，Risk 缺失时不降级 | `paper_trading._buy_order` | `test_r32e1_active_buy_provenance.py`（R1–R4） | ✅ |
| **Execution Dispatch hard block 的 owner provenance**：`dispatch_plan["blocked"]=True` 时 `allowed` 变假会把 `dispatch_gate` 抹成 `"none"`，分类必须用原始 `blocked` 事实 → `EXECUTION`；`verification_required`+`verification_rejected` 这类真实 `blocked=True/gate="none"` 状态不得被记成 `ENTRY`（纯 provenance 修正，不改交易行为） | `paper_trading._buy_order` | `test_r32e1_active_buy_provenance.py`（R5、R6 三类矩阵） | ✅ |
| 语义 mutation M-D1–M-D27（含 execution row 回流 risk_rejection、write-time authority 强制被删、exact-version 相等校验被删、canonical-shaped 伪造 policy 被接受、order 关联被换成 (account,code,side) 猜测、BUY 复合结论丢弃 order linkage、复合 rejection 被标成 RISK、Risk Authority 证据被改 owner、**dispatch hard block 退回只看被改写的 gate**） | `work/r32d_shadow_comparison_mutation_check.py` | 27/27 DETECTED，restore SHA256 PASS | ✅ |

## R32-D Active/Challenger 比对证据（IN REVIEW）

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 同一份 exact Active evidence + 同一 ShadowRun + 同一 spec 得到同一报告与同一 fingerprint；declared scope 顺序不影响 identity | `shadow_comparison.build_shadow_comparison` | `test_shadow_comparison.py`（D1） | ✅ |
| `ComparisonSpec.active_evidence_id` 必填且必须是合法 SHA-256；order IDs 本身不是 exact evidence identity | `shadow_comparison.ComparisonSpec` | `test_shadow_comparison.py`（D13） | ✅ |
| Active evidence 只有唯一 capture owner `capture_active_comparison_evidence(conn, active_order_ids=...)`：只依赖显式 order IDs，不接收 ComparisonSpec；旧 spec 驱动签名已删除且无 caller | `shadow_comparison_service` | `test_shadow_comparison.py`（D20、D20b） | ✅ |
| owner 原始 admission 分数经 capture → observation → 持久化 report 全程走 canonical `admission_score` 键（88.5），report 内不出现裸业务键 `score` | `shadow_comparison_service._admission_evidence`、`shadow_comparison._decision_dimension` | `test_shadow_comparison.py`（D19、D12） | ✅ |
| 同一 order id 的行被原地更新（filled_qty/amount/status/execution_evidence）后，按旧 fingerprint 调 production comparison 必须 `active_evidence_fingerprint_mismatch` fail closed 且不写 report；显式重声明新 fingerprint 才产生**不同** report | `shadow_comparison_service.build_and_append_comparison` | `test_shadow_comparison.py`（D13） | ✅ |
| 持久化 report 保存完整 canonical Active envelope（source table / order IDs / schema version / source fingerprint / 本次消费的 exact 投影），可自行重算 source fingerprint | `shadow_comparison.ShadowComparisonReport.active_evidence` | `test_shadow_comparison.py`（D1b、D13） | ✅ |
| report fingerprint 覆盖 exact Active evidence identity 与 exact ShadowRun identity：任一改变即改变 report identity | `shadow_comparison.build_shadow_comparison` | `test_shadow_comparison.py`（D1b） | ✅ |
| 七个共享环境维度（session、decision instant、market policy、market snapshot、quote、tradability、execution ruleset）逐维相等是硬前提；任一维不同 → UNAVAILABLE + `environment_mismatch`，且不计算任何 delta | `shadow_comparison.build_shadow_comparison` | `test_shadow_comparison.py`（D2 逐维） | ✅ |
| 不用 `ComparableRuntimeContext.context_fingerprint` 全等做比较判据；两腿策略专属差异是正常的 | `shadow_comparison._environment_identity` | `test_shadow_comparison.py`（D2/D1） | ✅ |
| Active runtime context 不可用时不重建：报告 UNAVAILABLE + 稳定 reason，coverage 0.0 | `shadow_comparison_service`、`shadow_comparison._active_leg_reasons` | `test_shadow_comparison.py`（D3、D15） | ✅ |
| 缺失 observation 记 `MISSING`，绝不等同 false / rejected / zero；不按 DB row order 决定顺序 | `shadow_comparison._observation_comparison` | `test_shadow_comparison.py`（D4、D18） | ✅ |
| 容器存在 ≠ owner 证据存在：每腿给出 `PRESENT / NOT_APPLICABLE / MISSING / UNAVAILABLE`；只有两腿 PRESENT 才是 AVAILABLE | `shadow_comparison._leg_dimension` | `test_shadow_comparison.py`（D14、D16、D18） | ✅ |
| Challenger 只有真实 `execution` Mapping 才算 execution evidence；`candidate` 存在而 `execution=None` 时按 owner 证据区分 NOT_APPLICABLE / MISSING，且不伪造 0 | `shadow_comparison._execution_dimension` | `test_shadow_comparison.py`（D14、D18） | ✅ |
| fill quantity 只能来自 owner：owner 明确产出 0 才报 0；缺失恒为 `None`，聚合在无可比 observation 时返回 `None` | `shadow_comparison._execution_dimension`、`_execution_aggregate` | `test_shadow_comparison.py`（D15b、D18） | ✅ |
| Active execution evidence 缺失时不算完整（environment 与执行结果同属一份 owner envelope） | `shadow_comparison_service` | `test_shadow_comparison.py`（D15） | ✅ |
| Active 无 order-linked Risk Authority 证据 → risk rejection 记 UNAVAILABLE；order status/reason 只作为独立 lifecycle 事实并显式标注不作为 admission/execution/risk 证据 | `shadow_comparison._risk_dimension`、`_order_lifecycle` | `test_shadow_comparison.py`（D17） | ✅ |
| coverage 分母恒为显式 expected：coverage ratio 与 `available/expected` 不一致直接拒绝构造 | `shadow_comparison.CoverageEvidence` | `test_shadow_comparison.py`（D5、D4） | ✅ |
| 从 exact evidence 重放：改变 current 策略表、`policy_for`、现行行情与 DB 路径后 fingerprint 不变；显式 ID 缺失时 fail closed，不回退"最新一条" | `shadow_comparison_service`、`shadow_run_repository.get_run` | `test_shadow_comparison.py`（D6、D7b） | ✅ |
| 纯层不打开 DB、不读 provider/cache/archive、不取机器时间、不存在 latest/current/head/find 入口 | `shadow_comparison`（结构证明） | `test_shadow_comparison.py`（D7） | ✅ |
| declared risk identity 必须标 `DECLARED`，绝不提升为 owner-verified；entry policy 标 `OWNER_ISSUED`；报告内不出现 `OWNER_VERIFIED` | `shadow_comparison._provenance_map`、`_risk_dimension` | `test_shadow_comparison.py`（D8） | ✅ |
| Performance 只用同一 frozen environment 的 quote 估值；缺 exact 持仓估值 → UNAVAILABLE，不读 latest quote；drawdown 恒 UNAVAILABLE | `shadow_comparison._performance` | `test_shadow_comparison.py`（D9、D9b） | ✅ |
| Turnover：Active normalized 因缺同语义分母恒 UNAVAILABLE（不读 current NAV）；Shadow 分母 = `ShadowRunSpec.reference_capital` | `shadow_comparison._turnover` | `test_shadow_comparison.py`（D1/D11） | ✅ |
| report 追加幂等（重复追加 row=1 且内容一致）；内容篡改即指纹不符报错；UPDATE/DELETE 被 append-only guard 拒绝 | `shadow_comparison_repository`、migration v25 | `test_shadow_comparison.py`（D10） | ✅ |
| 比对前后正式账本与 `shadow_runs` 逐字节不变；只新增一条 `shadow_comparison_reports` | `shadow_comparison_service.build_and_append_comparison` | `test_shadow_comparison.py`（D11） | ✅ |
| 报告不含 winner / promote / overall score / ranking 等业务结论字段（`score` 是精确键黑名单，owner 原始分数一律带前缀命名） | `shadow_comparison.ShadowComparisonReport` | `test_shadow_comparison.py`（D12，字段级黑名单 + 文本扫描） | ✅ |
| 比对报告 DDL 单一事实来源（`paper_schema_migrations.ensure_shadow_comparison_reports`）；migration v25 与 `init_db` 两条路径调用同一函数 | `paper_schema_migrations`、`db_migrate`、`paper_trading.init_db` | `test_shadow_comparison.py`（v25 DDL owner、existing ledger init_db） | ✅ |
| 语义 mutation M-D1–M-D16（环境相等、缺失即 false、coverage 失真、latest 回退、指纹丢 Active/Shadow 身份、declared→verified、缺估值不 fail closed、spec 可无 fingerprint、跳过 fingerprint 相等、容器即证据、None→0、order status 当 risk 证据、admission_score 退回旧键、capture 指纹脱离行内容、capture 重新接受 spec 参数） | `work/r32d_shadow_comparison_mutation_check.py` | 16/16 DETECTED，restore SHA256 PASS | ✅ |
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

## 决策审计序列化契约（issue #124）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 快照序列化单一真相源（实现只存在于审计模块；`paper_trading` 只保留兼容 facade，六个符号为别名、两个入口为委托，不得再复制实现） | `paper_decision_audit.build_decision_snapshot`、`paper_trading._decision_snapshot`、`_with_decision_snapshot` | `test_paper_decision_audit_facade.py`（`CompatibilityFacadeSurfaceTests`、`DecisionAuditArchitectureGuardTests`） | ✅ |
| 快照输出契约冻结（`decision-snapshot-v1` envelope 逐字段 golden：K 线 120 根窗口与 `omitted_rows` 计数、future row 排除、NaN→null 且缺失证据保持 `null`、自定义 K 线列、因子加权贡献、model / signal / entry_model 回退、quote 校验门、news 公告时间） | `paper_decision_audit.build_decision_snapshot`、`_snapshot_kline`、`_snapshot_factor_evidence`、`snapshot_safe` | `test_paper_decision_audit.py`（契约测试 + `GOLDEN_ENVELOPE`） | ✅ |
| runtime 依赖调用时注入（`kline_loader` / `news_scan_meta` / `risk_version` / `now_fn` 必须取**当前**模块属性，不得 import 时冻结；facade 行为上确实使用 patched 值，且不修改调用方 payload） | `paper_trading._decision_snapshot`、`_with_decision_snapshot` | `test_paper_decision_audit_facade.py`（`DecisionSnapshotFacadeDelegationTests`、`WithDecisionSnapshotFacadeDelegationTests`） | ✅ |
| 审计模块无副作用（不 import `paper_trading`；顶层依赖仅 stdlib + pandas；无 DB 写、无网络 I/O） | `paper_decision_audit` | `test_paper_decision_audit_facade.py`（`DecisionAuditArchitectureGuardTests`） | ✅ |
| 负向变异验证（N1–N18 必须让守卫变红：N1 冻结时钟、N2 冻结 news scan meta、N3 丢掉 kline loader 注入、N4 硬编码 risk version、N5 丢掉 `_with_decision_snapshot` 的 loader 注入、N6 改名复制回一套 serializer、N7 审计模块 import `paper_trading`、N8 审计模块做 DB IO、N9 审计模块调用写侧方法、N10 K 线窗口 120→121、N11 关掉 future row 排除、N12 NaN 字符串化、N13 不再拒绝非法 replay 日期、N14 就地改写调用方 payload、N15 删掉 `strategy_id` 补全、N16 存储根数差一、N17 忽略显式 `decision_at`、N18 把 serializer 复制到无关生产模块（`decision_context.py`），触发全仓 `test_serializer_contract_keys_live_only_in_the_audit_module` 守卫 ⇒ 合计 18/18） | `test_paper_decision_audit_facade.DecisionAuditArchitectureGuardTests.test_serializer_contract_keys_live_only_in_the_audit_module` | 手工执行 N1–N18 变异脚本（源码级变异逐字节备份 + `finally` 还原 + sha256 校验；见 PR #126 描述） | ✅ |

## 纸盘账户声明边界（Extract Paper Account Specs Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 声明内容逐字段冻结（五套内置 spec / 风格 / 风险画像 / 保守回退与抽取前一致；golden 为独立字面量，不由生产表反推） | `paper_account_specs.ACCOUNT_SPECS`、`STYLE_PROFILES`、`RISK_PROFILES`、`UNKNOWN_USER_SPEC` | `test_paper_account_specs.py`（`BuiltinSpecContractTests`、`StyleAndRiskProfileContractTests`） | ✅ |
| 声明键序是契约（内置 id 顺序即分配层 `account_order` 与仪表盘行序，不得重排） | `paper_account_specs.ACCOUNT_SPECS`、`builtin_account_ids` | `test_paper_account_specs.py`（`BuiltinSpecContractTests.test_declaration_order_is_frozen`） | ✅ |
| 唯一解析口（内置 → 声明层；用户策略 → 注册表 RuntimeContext 派生；未知 → 保守回退，绝不 KeyError、绝不静默映射到内置身份） | `paper_trading._spec_for`、`paper_account_specs.builtin_spec` / `fallback_spec` | `test_paper_account_specs.py`（`SpecResolutionContractTests`、`UserStrategyResolutionTests`）、`test_strategy_spec_resolution.py` | ✅ |
| 口径分离（`ACTIVE_ACCOUNT_IDS`/`ACTIVE_ACCOUNT_SPECS` 是注册表 active ∩ 声明键的**投影**，定义点留在权威层；声明模块不持有生命周期 / 版本 / 周期所有权 / 执行资格真相） | `paper_trading.ACTIVE_ACCOUNT_IDS`、`ACTIVE_ACCOUNT_SPECS` | `test_paper_account_specs.py`（`RegistryAgreementTests`） | ✅ |
| 归档 / 回放可解析性（历史账本解码不依赖注册表当前生命周期；孤儿账户兜底不崩仪表盘） | `paper_account_specs.ACCOUNT_SPECS`、`paper_trading._spec_for` | `test_paper_account_specs.py`（`ArchiveReplayResolvabilityTests`）、`test_strategy_archive_replay.py` | ✅ |
| 可变隔离（所有查询访问器返回独立副本，含嵌套列表；调用方改动不污染声明真相） | `paper_account_specs.builtin_spec` / `fallback_spec` / `style_profile` / `risk_profile` | `test_paper_account_specs.py`（`MutableIsolationTests`） | ✅ |
| 兼容 facade 只允许别名 / 委托（三个表 + 回退 + 两个版本常量是同一对象或同值别名；`_spec_for` 体内必须出现声明层委托且不得内联第二套解析） | `paper_trading.ACCOUNT_SPECS`、`STYLE_PROFILES`、`RISK_PROFILES`、`_UNKNOWN_USER_SPEC`、`NEW_STRATEGY_VERSION`、`MAIN_FORCE_STRATEGY_VERSION`、`_spec_for` | `test_paper_account_specs.py`（`CompatibilityFacadeTests`） | ✅ |
| 架构守卫（声明模块不 import `paper_trading` / 注册表 / 运行时 / 账本 / 网络 / 调度 / 执行；import 根白名单；无 DB·网络·订单副作用调用；无 import 期调用即无副作用） | `paper_account_specs` | `test_paper_account_specs.py`（`SpecsArchitectureGuardTests`） | ✅ |
| 单一实现守卫（AST 按"账户 spec 表形状"全仓扫描：声明模块**有且只有**一张，其他生产模块不得出现第二张；先证检测器非空性再证别处为空） | `paper_account_specs.ACCOUNT_SPECS` | `test_paper_account_specs.py`（`SpecsArchitectureGuardTests.test_detector_finds_the_real_table_in_the_specs_module`、`test_account_spec_table_lives_only_in_the_specs_module`、`test_paper_trading_holds_no_duplicated_spec_values`） | ✅ |
| 负向变异验证（N1–N10 必须让守卫变红：N1 改内置 `max_positions`、N2 改内置风险画像键、N3 内置查找退化为 `ACCOUNT_SPECS[id]` 直接下标、N4 把完整声明表回抄进 `paper_trading`、N5 声明模块反向 import `paper_trading`、N6 在声明模块 import 期冻结注册表 active 集合、N7 让归档/非 active 策略不可解析、N8 未知账户静默映射到内置身份、N9 查询返回模块级可变 dict、N10 引入 DB 写 / 网络副作用 ⇒ 合计 10/10） | — | 手工执行 N1–N10 变异脚本（源码级变异逐字节备份 + `finally` 还原 + `git hash-object` 校验；见 PR 描述） | ✅ |

## 周期所有权解析边界（Extract Cycle Ownership Resolver）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 经济所有权口径（`enabled_strategies` ∩ `paper_accounts.cycle_id == 目标周期`；只启用未挂接、只挂接未启用都不算所有权；别周期账户不得进入） | `paper_cycle_ownership.cycle_ledger_filter` / `cycle_ledger_rows` / `cycle_ledger_ids` | `test_paper_cycle_ownership.py`（`EconomicOwnershipContractTests`） | ✅ |
| lifecycle pause 不改变经济所有权（pause 期间仍计入共享池合计与 NAV；`configure_capital` 仍分份额；resume 不凭空放大资本） | `paper_cycle_ownership.cycle_ledger_ids` | `test_paper_cycle_ownership.py`（`EconomicOwnershipContractTests.test_lifecycle_pause_does_not_remove_economic_ownership`）、`test_cycle_ledger_ownership.py` | ✅ |
| 执行资格 = 经济所有权 − lifecycle pause（pause 立即剔除执行层但不解绑 `cycle_id` / 不清零资金 / 不改写 `enabled_strategies`；resume 恢复） | `paper_cycle_ownership.cycle_participant_resolution` / `current_cycle_participant_ids` / `execution_participant_ids` | `test_paper_cycle_ownership.py`（`ExecutionParticipationContractTests`）、`test_cycle_participant_resolver.py`、`test_strategy_product_line_e2e.py` | ✅ |
| 显式 idle 周期（`enabled_strategies == []` → `source = "cycle_idle"`，零参与者，绝不回落内置五套） | `paper_cycle_ownership.cycle_participant_resolution` | `test_paper_cycle_ownership.py`（`IdleAndLegacyFallbackContractTests.test_explicit_empty_enabled_set_yields_zero_participants`、`test_idle_cycle_never_falls_back_to_builtin_scope`）、`test_cycle_participant_resolver.py` | ✅ |
| 未配置 / 未挂接 / 无周期 / 无连接的 legacy 回退（`cycle_not_configured`、`cycle_enabled_unbound_fallback`、`no_cycle`、`no_conn` 逐字保留；与 idle **不得合并**） | `paper_cycle_ownership.cycle_participant_resolution` | `test_paper_cycle_ownership.py`（`IdleAndLegacyFallbackContractTests`） | ✅ |
| 判定来源与版本元数据（`ids` / `source` / `enabled` / `bound` / `paused` / `cycle_id` / `version`） | `paper_cycle_ownership.cycle_participant_resolution` | `test_paper_cycle_ownership.py`（`test_resolution_metadata_is_preserved`） | ✅ |
| 参与者顺序是契约（`dict.fromkeys` 保序 + `ORDER BY id`；集合化 / 逆序即失败） | `paper_cycle_ownership.cycle_participant_resolution` / `cycle_ledger_rows` | `test_paper_cycle_ownership.py`（`test_participant_order_is_deterministic_and_follows_enabled_order`、`test_ledger_rows_are_ordered_by_id`） | ✅ |
| 只读（无写 SQL / 无 PRAGMA；sqlite authorizer 证明解析期间零 INSERT·UPDATE·DELETE·DDL，且行快照前后一致） | `paper_cycle_ownership` | `test_paper_cycle_ownership.py`（`ReadOnlyResolverTests`、`OwnershipArchitectureGuardTests.test_ownership_module_emits_no_write_sql`） | ✅ |
| 行读取不假设 `row_factory`（`cursor.description` + `zip` 自组装 dict，裸连接亦可用；单行 `paper_cycles` 查询同样归一化） | `paper_cycle_ownership._rows` / `_row` | `test_paper_cycle_ownership.py`（`test_row_reader_does_not_assume_the_connection_row_factory`、`test_bare_connection_execution_resolution_uses_cycle_snapshot`） | ✅ |
| 风控退出资格 ≠ 执行资格（paused / 已退出当前周期但仍有 `remaining_qty>0` 的账户必须继续被风控扫描） | `paper_risk_exit_eligibility.risk_exit_account_ids`（`paper_trading._risk_exit_account_ids` facade） | `test_paper_risk_exit_eligibility.py`、`test_paper_cycle_ownership.py` | ✅ |
| 兼容 facade 只允许别名 / 委托（`paper_trading` 内同名符号必须委托到解析器；函数体不得内联第二套所有权实现；注入的注册表作用域必须**调用时**读取，monkeypatch 立即生效） | `paper_trading._active_cycle_filter` / `_shared_account_rows` / `cycle_ledger_ids` / `execution_participant_ids` / `current_cycle_participant_ids` / `_cycle_participant_resolution` / `_lifecycle_paused_ids` | `test_paper_cycle_ownership.py`（`CompatibilityFacadeTests`） | ✅ |
| 架构守卫（解析器不 import `paper_trading` / 网络 / 订单 / 注册表 / 执行；import 根白名单；无下单·建周期·开户·归档·改状态调用；不携带执行·风控退出·注册表作用域权威；顶层无 `Call`） | `paper_cycle_ownership` | `test_paper_cycle_ownership.py`（`OwnershipArchitectureGuardTests`） | ✅ |
| 单一实现守卫（全仓唯一：读取周期快照启用集合的 `SELECT` 只允许出现在解析器；解析面函数名不得在别处重定义；先证检测器非空再证别处为空） | `paper_cycle_ownership.cycle_ledger_filter` | `test_paper_cycle_ownership.py`（`OwnershipArchitectureGuardTests.test_detector_finds_the_ownership_sql_in_the_ownership_module`、`test_cycle_ownership_sql_lives_only_in_the_ownership_module`、`test_no_other_module_redefines_the_resolution_surface`） | ✅ |
| 负向变异验证（N1–N13 必须让守卫变红：N1 账本所有权错误剔除 paused、N2 执行参与者不再剔除 paused、N3 执行参与者改用注册表 active 作用域、N4 显式空集错误回落内置五套、N5 忽略 `paper_accounts.cycle_id` 绑定、N6 删除 `cycle_not_configured` 回退、N7 删除未挂接周期回退、N8 参与者顺序漂移、N9 把解析器复制回 `paper_trading`、N10 解析器反向 import `paper_trading`、N11 解析器引入写操作、N12 风控退出被收窄为执行参与者、N13 单行 `paper_cycles` 查询退化为裸 `fetchone()` + 字符串键访问 ⇒ 合计 13/13） | — | 手工执行 N1–N13 变异脚本（源码级变异逐字节备份 + `finally` 还原 + `git hash-object` 校验；见 PR 描述） | ✅ |

## 共享现金账本边界（Extract Shared Cash Ledger Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 共享现金只聚合调用方已解析账户行 | `paper_shared_cash.shared_cash` | `test_paper_shared_cash.py`（`SharedCashAggregateTests`） | ✅ |
| 声明资本优先；无声明时沿用账户初始资本/现金回退并保持非负 | `paper_shared_cash.shared_initial_cash`、`paper_trading._shared_initial_cash` | `test_paper_shared_cash.py`（`SharedCashAggregateTests`、`SharedCashFacadeTests`） | ✅ |
| 借记顺序保持：preferred 优先，其余按现金降序；等现金保持输入顺序 | `paper_shared_cash.debit_shared_cash` | `test_paper_shared_cash.py`（`SharedCashDebitTests`） | ✅ |
| 借记资金不足先预检，失败不产生部分写入；负数/零值保持旧 fail-closed 行为 | `paper_shared_cash.debit_shared_cash` | `test_paper_shared_cash.py`（`SharedCashDebitTests`） | ✅ |
| 贷记只写明确目标账户，负数拒绝；零值保持旧的现金不变/时间戳更新行为 | `paper_shared_cash.credit_shared_cash` | `test_paper_shared_cash.py`（`SharedCashCreditTests`） | ✅ |
| 数据库副作用严格限制为 `paper_accounts.cash` 与 `updated_at` | `paper_shared_cash` | `test_paper_shared_cash.py`（`SharedCashCreditTests`、`SharedCashDebitTests`、`SharedCashArchitectureGuardTests`） | ✅ |
| 四个旧入口保留原签名并只做 runtime 注入后的委托，不改变调用方账户解析 | `paper_trading._shared_cash`、`_shared_initial_cash`、`_debit_shared_cash`、`_credit_shared_cash` | `test_paper_shared_cash.py`（`SharedCashFacadeTests`） | ✅ |
| 模块依赖与职责守卫：无周期/注册表/预约/敞口/风控事实，无反向 import，无 import 期副作用 | `paper_shared_cash`、`ARCHITECTURE.md` | `test_paper_shared_cash.py`（`SharedCashArchitectureGuardTests`） | ✅ |
| 负向变异验证（N1–N13 必须让守卫变红：N1 preferred 优先级、N2 现金降序、N3 资金不足预检、N4 预检后才写、N5 负数借记 fail-closed、N6 零借记无写入、N7 贷记目标不漂移、N8 负数贷记拒绝、N9 引入周期/所有权事实、N10 反向 import、N11 扩大 UPDATE 写入列、N12 facade 不再委托、N13 等现金 tie 顺序漂移 ⇒ 合计 13/13） | — | 手工执行 N1–N13 变异脚本（源码级逐字节备份 + `finally` 还原 + `git hash-object` 校验；见 PR 描述） | ✅ |

## 用户策略账户 provisioning 边界（Extract User Account Provisioning Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 缺失用户策略账户逐字段创建（paused、零初始资金/现金、无周期、当前 spec 字段、无 benchmark 起点） | `paper_user_account_provisioning.provision_user_accounts` | `test_paper_user_account_provisioning.py`（`ProvisioningContractTests`） | ✅ |
| 已有账户完全跳过；不调用 spec/clock/audit，不改任何字段 | `paper_user_account_provisioning.provision_user_accounts` | `ProvisioningContractTests.test_existing_account_is_untouched_and_skips_callbacks` | ✅ |
| 多账户保序、只创建缺失行、成功创建才写精确开户审计 | `paper_user_account_provisioning.provision_user_accounts` | `ProvisioningContractTests.test_multiple_accounts_only_missing_rows_follow_input_order` | ✅ |
| 空输入无 SQL 变更；重复调用无重复行/审计 | `paper_user_account_provisioning.provision_user_accounts` | `ProvisioningContractTests.test_empty_input_performs_no_sql_mutation`、`test_repeated_call_is_idempotent` | ✅ |
| spec / INSERT / audit 异常原样传播；模块不拥有 commit/rollback，外层 rollback 可恢复 | `paper_user_account_provisioning.provision_user_accounts` | `ProvisioningContractTests.test_spec_failure_propagates_without_insert_or_audit`、`test_insert_failure_propagates_without_audit`、`test_audit_failure_propagates_without_commit_or_rollback` | ✅ |
| facade 保留 `(conn)` 签名并在调用时注入当前参与资格、spec、时钟和审计依赖 | `paper_trading._ensure_user_strategy_accounts` | `paper_user_account_provisioning.py`（`FacadeContractTests`） | ✅ |
| 责任隔离与单一实现（stdlib-only、只写缺失 `paper_accounts`、不持有资格/周期/资金/执行真相、无 import 期调用） | `paper_user_account_provisioning` / `paper_trading` | `test_paper_user_account_provisioning.py`（`ArchitectureGuardTests`） | ✅ |
| 负向变异验证（N1–N15：状态、资金、周期、已有行、callback、审计、版本、越权查询/import/事务、facade 内联 SQL、benchmark 起点、周期/现金写入；每个真实源码变异均应被新增守卫抓红） | — | 手工执行变异脚本（逐字节备份、`finally` 恢复、sha256 校验；见 PR 描述） | ✅ |

## 用户策略周期挂接边界（Extract User Cycle Attachment Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| normal cycle 使用调用方已解析的 available capital；zero capital 仍 attach 且不造钱 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `test_paper_user_cycle_attachment.py`（`AttachContractTests`） | ✅ |
| legacy cycle 严格按 `cycle.capital / max(len(enabled_ids), 1)` 分配 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachContractTests.test_legacy_cycle_divides_by_enabled_ids_only` | ✅ |
| attach 字段逐字段 golden：cycle/status/spec/capital/benchmark/date/version/risk/max limits | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachContractTests.test_normal_cycle_uses_available_capital_and_golden_account_fields` | ✅ |
| attach 后只清理旧 `paper_nav` 并创建当日初始行 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachContractTests.test_attach_resets_paper_nav_to_one_current_initial_row` | ✅ |
| attach 后 parameter-version evidence 与 reason/params golden | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachContractTests.test_parameter_version_evidence_is_golden` | ✅ |
| 成功 attach audit event/detail golden；detach 不写该 audit | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachContractTests.test_attach_audit_event_and_detail_are_golden`、`DetachContractTests.test_detach_does_not_write_attachment_audit` | ✅ |
| disabled current-cycle user 精确 detach；只清 cycle/status/initial_cash/cash/updated_at | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `DetachContractTests.test_disabled_bound_current_cycle_detaches_exactly` | ✅ |
| detach 保留历史 `paper_nav` 与 `paper_parameter_versions`；显式 idle 不回落 builtin | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `DetachContractTests` | ✅ |
| lifecycle pause 不改变 caller 已传入的 economic attachment target | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachmentDecisionTests.test_lifecycle_pause_does_not_detach_when_caller_keeps_id_enabled` | ✅ |
| current-cycle 已挂账户不 refresh；other-cycle 账户不抢绑 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachmentDecisionTests.test_already_bound_to_current_cycle_is_not_refreshed`、`test_bound_to_another_cycle_is_not_stolen` | ✅ |
| 缺失账户跳过且不解析 spec；spec resolution 先于 detach decision；多账户按调用顺序 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `AttachmentDecisionTests` | ✅ |
| 裸 `row_factory=None` 与 `sqlite3.Row` 均可执行单行读取 | `paper_user_cycle_attachment._row` | `AttachmentDecisionTests.test_sqlite_row_and_bare_tuple_rows_are_both_supported` | ✅ |
| audit 异常传播，caller rollback 可恢复；模块不拥有事务 | `paper_user_cycle_attachment.reconcile_user_cycle_accounts` | `TransactionAndFacadeTests.test_audit_failure_propagates_and_caller_rollback_restores_state` | ✅ |
| `_ensure_cycle` 真实生产路径委托新模块，保留两次 version bind 与 shared cash 顺序 | `paper_trading._ensure_cycle` | `TransactionAndFacadeTests.test_production_ensure_cycle_delegates_between_two_version_binds` | ✅ |
| facade/orchestrator 保留 `all_user_ids`、`USP.user_known_ids`、builtin repair 与 callback 注入 | `paper_trading._ensure_cycle` | `TransactionAndFacadeTests.test_facade_passes_call_time_callbacks_to_attachment_module`、`ArchitectureGuardTests.test_facade_keeps_orchestration_owners_and_order_markers` | ✅ |
| 模块零 backend import、无 registry/lifecycle/cycle/shared-cash/order/fill/lots/transaction 越权，只写三张允许表 | `paper_user_cycle_attachment` | `ArchitectureGuardTests` | ✅ |
| user attachment 核心 SQL 只有一份，`paper_trading` 无第二套 attach/NAV/parameter 实现 | `paper_user_cycle_attachment` / `paper_trading` | `ArchitectureGuardTests.test_single_attachment_audit_implementation_and_no_inline_copy` | ✅ |
| 真实 mutation N1–N16 全部捕获：detach/status/cash/NAV/ownership/capital/zero/legacy/status/抢绑/refresh/parameter/audit/authority/transaction/bare-row | — | 手工执行 N1–N16 变异脚本（逐字节备份、`finally` 恢复、sha256 校验；见 PR 描述） | ✅ |

## 维护约定

1. 新增门禁必须先在本表加一行，再写测试；表格状态从 ⚠️ → ✅。
2. `⚠️` = 逻辑已实现但缺独立离线单测，是贡献者 `good first issue` 的优先候选。
3. demo 叙事用例（`test_demo_seed` / `test_demo_replay_golden`）作为引擎路径的端到端兜底回归。
# 资金预占 ledger 边界

`backend/test_paper_capital_reservations.py` 覆盖 reserved BUY 的跨周期聚合、账户金额与费用、排除自身、numeric clamp、裸 SQLite/`sqlite3.Row`、创建与重算、consumed 防重复、资金不足零变更、epsilon 容差、lazy active-cycle/shared-cash/clock 依赖、终态幂等，以及三个 legacy facade 的签名与单一实现守卫。模块禁止反向依赖、订单/持仓/周期 SQL、事务控制和 allocation/sizing/execution 逻辑。

`_reconcile_signal_order_states` 的 reservation UPDATE 属于明确允许的 crash-recovery exception；架构测试用 AST 函数级检测，先证明 recovery exception 存在，再拒绝其它 runtime 直写函数。N17 把 `now_fn()` 提前到新 reservation 的 active-cycle 解析之前，callback-order 回归必须失败；每轮真实突变后均逐字节恢复并核验 SHA-256。

## 固定周期本金归属边界（Extract Fixed-Cycle Capital Attribution Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| available capital 先解析 caller ownership filter，再读取其他账户 `initial_cash`；target `id<>?` 排除，zero-initial row 仍计数，非 ownership row 不计入 | `paper_cycle_capital.available_cycle_ledger_capital` | `test_paper_cycle_capital.py`（available contract tests） | ✅ |
| available 无 ownership row 使用 `cycle.capital / max(len(builtin_account_ids), 1)`，有 row 才做剩余本金 `max(0.0, ...)` | `paper_cycle_capital.available_cycle_ledger_capital` | `test_paper_cycle_capital.py` | ✅ |
| late-join 只按 funded `initial_cash>0` sleeves 求平均并 `round(..., 2)`；无 funded row 使用 rounded fallback；不读取 `cash` | `paper_cycle_capital.late_join_reference_capital` | `test_paper_cycle_capital.py`（late-join contract tests） | ✅ |
| callback / SQL 顺序、裸 SQLite 与 `sqlite3.Row` 兼容、只读与 stdlib/import 边界 | `paper_cycle_capital` | `test_paper_cycle_capital.py` | ✅ |
| 两个 `paper_trading` compatibility facade 保持原签名，并在调用时解析 `_active_cycle_filter`、`ACTIVE_ACCOUNT_IDS`、`_num` | `paper_trading._available_cycle_ledger_capital` / `_late_join_reference_capital` | `test_paper_cycle_capital.py`（facade tests） | ✅ |
| 真实源码变异 N1–N12 全部捕获；each mutation restored byte-for-byte and SHA-256 verified before proceeding to the next mutation; final restore/hash also verified | `paper_cycle_capital` / `paper_trading` | local workspace mutation tooling/evidence: `work/pr_cycle_capital_negative_check.py` | ✅ |

## 待成交席位占用 read model 边界（Extract Pending Slot Occupancy Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 可执行待买订单（manual/strategy + buy + pending_limit/retry statuses）计入席位占用 | `paper_slot_occupancy.pending_position_slots` | `test_paper_slot_occupancy.py`（`SlotOccupancyFilteringTests.test_order_filtering_counts_executable_buys`） | ✅ |
| 卖单与非 manual/strategy 来源完全忽略，不占席位 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyFilteringTests.test_order_filtering_ignores_sell_orders`、`test_order_filtering_ignores_other_origins` | ✅ |
| 终态（filled/cancelled/rejected/released/expired）与其它状态不占席位 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyFilteringTests.test_order_filtering_ignores_terminal_and_non_occupying_statuses` | ✅ |
| 关键不变量：deferred_capacity 与 entry_frozen_waitlist 仅为排队标记，不占席位 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyFilteringTests.test_waitlist_and_deferred_do_not_occupy_slots`、`test_capacity_waitlist_slots.py` | ✅ |
| 未在 occupying_statuses tuple 里的 pending-like 状态不占席位；空状态集合直接返回空集 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyFilteringTests.test_pending_like_statuses_outside_occupying_tuple_ignored`、`test_empty_occupying_statuses_returns_empty_set` | ✅ |
| 空状态与非法排除键交叉组合：空 occupying_statuses 仍先校验 exclude_order_key 合法性并抛出 ValueError，不绕过校验 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyFilteringTests.test_empty_statuses_still_validate_invalid_exclude_before_return` | ✅ |
| 席位身份为 (account_id, code)；同账户同代码多单去重为 1 席位，不同账户同代码为独立席位 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyIdentityAndSuppressionTests.test_distinct_identity_same_account_same_code_is_one_slot`、`test_distinct_identity_different_accounts_same_code_are_two_slots` | ✅ |
| 既有持仓加仓抑制：已有 >= LOT_SIZE 完整持仓的 (account, code) 待买单不占用新席位 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyIdentityAndSuppressionTests.test_existing_full_position_suppresses_pending_order` | ✅ |
| 碎股边界严格判定：qty=99 不抑制，qty=100 抑制，qty=100.9 取整抑制 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyIdentityAndSuppressionTests.test_sub_lot_boundary_qty_99_does_not_suppress`、`test_sub_lot_boundary_qty_100_suppresses`、`test_sub_lot_boundary_fractional_qty_100_9_suppresses` | ✅ |
| exclude_order_key 严格按 paper_orders.id 排除，数字字符串先 int() 转换，非法字符串抛出 ValueError 且不执行查询 | `paper_slot_occupancy.pending_position_slots` | `SlotOccupancyIdentityAndSuppressionTests.test_exclude_order_key_filters_exact_id_and_coerces_string`、`test_exclude_order_key_invalid_string_raises_value_error_without_query` | ✅ |
| facade 保留原签名 `(conn, positions=None, exclude_order_key=None)`；显式 `positions=[]` 不触发回退，`positions=None` 触发 `_position_rows(conn)` | `paper_trading._pending_position_slots` | `SlotOccupancyFacadeContractTests.test_facade_signature_exact_match`、`test_explicit_empty_positions_skips_position_rows_sentinel`、`test_none_positions_invokes_position_rows_sentinel` | ✅ |
| facade 调用时向新模块注入当前 occupying_statuses、lot_size、_num 与 _rows | `paper_trading._pending_position_slots` | `SlotOccupancyFacadeContractTests.test_call_time_dependency_injection` | ✅ |
| 生产调用方（dashboard_queries、manual_orders、_plan_strategy_entry、_resolve_slot_borrow_candidate）统一经 facade 获取席位占用 | `dashboard_queries`、`manual_orders`、`paper_trading` | `SlotOccupancyFacadeContractTests.test_production_consumers_parity` | ✅ |
| 模块纯 stdlib、只读查询 paper_orders、无写 SQL、无事务控制、不导入 paper_trading；facade 内部无内联 SQL | `paper_slot_occupancy` / `paper_trading` | `SlotOccupancyArchitectureGuardTests` | ✅ |
| 架构文档明确区分 paper_slot_occupancy 与 paper_slot_service，冻结两者不同职责 | `ARCHITECTURE.md` | `SlotOccupancyArchitectureGuardTests.test_architecture_notes_distinguishes_slot_service_and_occupancy` | ✅ |
| 真实源码变异 N1–N19 全部被测试捕获（19/19 caught，Undetected: 0） | — | `work/pr_slot_occupancy_negative_check.py`（每轮真实变异、逐字节备份与 sha256 还原核验） | ✅ |

## 风控退出资格边界（Extract Risk Exit Eligibility Boundary）

| 场景 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 契约 A：执行参与者无持仓，仍属于风控退出范围 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `test_paper_risk_exit_eligibility.py`（`RiskExitEligibilityContractTests.test_A_execution_participant_without_holdings_is_eligible`） | ✅ |
| 契约 B：paused 账户失去执行资格，但有 remaining lots（`remaining_qty > 0`）必须继续进入风控扫描 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_B_paused_account_with_positive_remaining_lots_is_eligible` | ✅ |
| 契约 C：已 archived/retired 账户不在基础执行范围，但有 remaining lots 必须继续进入风控扫描 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_C_archived_retired_account_with_positive_remaining_lots_is_eligible` | ✅ |
| 契约 D：当前周期外历史账户有 remaining lots 必须继续进入风控扫描 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_D_account_outside_current_cycle_with_positive_remaining_lots_is_eligible` | ✅ |
| 契约 E：当前周期外历史账户 zero remaining（`remaining_qty == 0`）不得进入风控退出资格 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_E_account_outside_current_cycle_with_zero_remaining_is_not_eligible` | ✅ |
| 契约 F：负数持仓（`remaining_qty <= 0`）不得制造风控退出资格 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_F_negative_remaining_qty_does_not_confer_eligibility` | ✅ |
| 契约 G：同账户多笔 lots 正确去重，返回集合只含唯一账户 ID | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_G_multiple_lots_for_same_account_are_deduplicated` | ✅ |
| 契约 H：多账户混合（执行中无仓、执行中有仓、非执行有仓、非执行零仓/负仓）并集正确 | `paper_risk_exit_eligibility.risk_exit_account_ids` | `RiskExitEligibilityContractTests.test_H_mixed_accounts_union_correctness` | ✅ |
| 契约 I：facade 原签名保持 `(conn, status="running")`，返回值保持 `set[str]` | `paper_trading._risk_exit_account_ids` | `RiskExitEligibilityFacadeContractTests.test_I_facade_signature_preserved` | ✅ |
| 契约 J：status 参数旧语义逐字保持（status="paused" 与 status=None 正确传递给 `_active_account_ids`） | `paper_trading._risk_exit_account_ids` | `RiskExitEligibilityFacadeContractTests.test_J_status_parameter_passthrough` | ✅ |
| 契约 K：facade 使用调用时依赖解析，monkeypatch `_active_account_ids` 立即生效 | `paper_trading._risk_exit_account_ids` | `RiskExitEligibilityFacadeContractTests.test_K_facade_uses_call_time_dependency_resolution` | ✅ |
| 契约 L：新模块纯只读，sqlite authorizer 拦截写操作码，源码无写 SQL 关键字 | `paper_risk_exit_eligibility` | `RiskExitEligibilityArchitectureGuardTests.test_L_module_is_read_only` | ✅ |
| 契约 M：新模块纯 stdlib，无反向导入 `paper_trading` | `paper_risk_exit_eligibility` | `RiskExitEligibilityArchitectureGuardTests.test_M_module_stdlib_only_and_no_reverse_import` | ✅ |
| 契约 N：新模块不重新实现周期所有权或执行参与者逻辑，无跨边界表查询 | `paper_risk_exit_eligibility` | `RiskExitEligibilityArchitectureGuardTests.test_N_module_does_not_reimplement_cycle_or_execution` | ✅ |
| 契约 P3：保持 exact legacy account ID 语义，不得 strip()、normalize 或 validate 账户 ID；base_account_ids 保留原始对象身份；持仓行 raw truthy value 保留 str(val)（包括带空格字符串） | `paper_risk_exit_eligibility.risk_exit_account_ids` | `test_paper_risk_exit_eligibility.py`（`RiskExitEligibilityContractTests.test_exact_legacy_account_id_semantics`、`test_raw_account_id_extraction_without_normalization`） | ✅ |
| 契约 P3-2：保持生产 facade 对裸 SQLite 连接（row_factory=None）及 sqlite3.Row 的完全双向兼容；facade 不强制注入 repository 行适配器 | `paper_trading._risk_exit_account_ids` | `test_paper_risk_exit_eligibility.py`（`RiskExitEligibilityFacadeContractTests.test_facade_bare_sqlite_connection_compatibility`、`test_facade_sqlite_row_connection_compatibility`） | ✅ |
| 真实源码变异 N1–N12 全部被测试捕获（12/12 caught，Undetected: 0） | — | `work/pr_risk_exit_eligibility_negative_check.py`（每轮真实变异、逐字节备份与 sha256 还原核验） | ✅ |

### 风控退出真实生产链路回归矩阵（PR #135 加固）

| 场景 / 契约 | 涉及组件 / 链路 | 对应自动化测试 | 状态 |
| --- | --- | --- | --- |
| 场景 A：正常执行中账户 + 真实持仓 + 触发风控，全链路生成卖单并完成成交扣减 | `_monitor_risk_impl` → `_consume_available_lots` → `_credit_shared_cash` | `test_paper_risk_exit_production_path.py`（`TestPaperRiskExitProductionPath.test_A_normal_running_account_full_risk_exit_pipeline`） | ✅ |
| 场景 B：paused 账户无 buy 资格，但存量持仓继续拥有风控退出资格，平仓归零后退出风控资格 | `_active_account_rows` / `_risk_exit_account_ids` | `TestPaperRiskExitProductionPath.test_B_paused_account_with_holdings_has_exit_eligibility_but_no_buy_eligibility` | ✅ |
| 场景 C：archived / out-of-cycle 账户存量持仓继续拥有退出路径，释放存量持仓 | `paper_accounts.status='archived'` / `_risk_exit_account_ids` | `TestPaperRiskExitProductionPath.test_C_archived_out_of_cycle_account_executes_risk_exit` | ✅ |
| 场景 D：paused 与 archived / out-of-cycle 账户 0 持仓均严格不进入风控退出订单生成阶段 | `_risk_exit_account_ids` / `_position_rows` | `TestPaperRiskExitProductionPath.test_D_paused_or_archived_account_with_zero_holdings_never_enters_risk_exit` | ✅ |
| 场景 E：多 lot 持仓场景严格 FIFO 扣减，成本计算与扣减数量不超扣 | `_consume_available_lots` | `TestPaperRiskExitProductionPath.test_E_multi_lot_fifo_consumption_without_over_deduction` | ✅ |
| 场景 F：部分成交（partial fill / trim）精准扣减 remaining_qty，剩余持仓保留风控退出资格 | `_sell_plan` → `_consume_available_lots` | `TestPaperRiskExitProductionPath.test_F_partial_fill_deducts_accurately_and_preserves_exit_eligibility` | ✅ |
| 场景 G：完全成交（full fill）后敞口归零，后续 review 轮次绝不重复下达退出卖单 | `_position_rows` / `paper_position_lots` | `TestPaperRiskExitProductionPath.test_G_full_fill_clears_exposure_and_subsequent_review_does_not_duplicate` | ✅ |
| 场景 H：runner 幂等性，同一调度周期内重复调用返回 already_scanned，无双花/多扣 | `risk_scan_state` / `monitor_risk` | `TestPaperRiskExitProductionPath.test_H_runner_idempotency_within_same_minute` | ✅ |
| 场景 I：已存在未完成跌停卖单（unfilled_limit_down）在冷却期内抑制重复下单（unfilled_limit_down_wait） | `paper_orders` cooldown check | `TestPaperRiskExitProductionPath.test_I_unfilled_limit_down_cooldown_suppresses_duplicate_orders` | ✅ |
| 场景 J：买入侧容量冻结（`PAPER_ENTRY_FREEZE`）互不干扰，风控退出 SELL 绝不受阻 | `_entry_freeze_status` / `_monitor_risk_impl` | `TestPaperRiskExitProductionPath.test_J_entry_freeze_env_does_not_block_risk_exit` | ✅ |
| 场景 K：净回款（amount - fees）精确释放归还账户现金与共享池账本 | `_credit_shared_cash` | `TestPaperRiskExitProductionPath.test_K_cash_release_consistency_matches_order_fill_net_proceeds` | ✅ |
| 场景 L：风控退出 SELL 委托不占用买入槽位（`pending_position_slots` 仅识别 `side='buy'`） | `paper_slot_occupancy.pending_position_slots` | `TestPaperRiskExitProductionPath.test_L_risk_exit_sell_orders_do_not_occupy_buy_slots` | ✅ |
| 场景 M：跨 6 张表强一致性与订单-成交关系校验（accounts, positions, lots, orders, fills, reservations; fills ↔ orders 双向 join 且无对向 side 错连） | 6 张核心账本表 | `TestPaperRiskExitProductionPath.test_M_strong_consistency_state_transition_golden_snapshot` | ✅ |
| 场景 N：中间步骤抛异常触发 savepoint 回滚，无脏 lot，无虚构订单/成交/资金，安全转入 execution_retry | `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` | `TestPaperRiskExitProductionPath.test_N_failure_branch_rolls_back_savepoint_without_corrupting_lots` | ✅ |
| 场景 O：顶层调度生产入口 `run_slot("risk", ...)` 成功闭环并记录调度状态 | `paper_trading.run_slot` | `TestPaperRiskExitProductionPath.test_O_golden_run_slot_risk_production_entrypoint` | ✅ |
| 真实生产源码变异 N1–N12 全部被测试捕获（12/12 caught，Undetected: 0） | `paper_risk_exit_eligibility` / `paper_slot_occupancy` / `paper_trading` | 本地突变套件 `pr_risk_exit_production_negative_check.py`（逐项注入、逐字节核验与 sha256 还原） | ✅ |

## 执行真实性证据层（Execution Reality Layer）

对应目标："选出来 ≠ 能成交 ≠ 成交价格可信"。本层建立在 PR149 的
`selection_tradability` 之上，**不修改**其任何判定口径；`test_execution_outcome.py`
的 `Pr149ContractPreservationTests` 会把"改动 PR149 契约"直接测红。

| 场景 / 契约 | 实现位置 | 回归用例 | 状态 |
| --- | --- | --- | --- |
| 三态证据：`known` 必须携带非 None 值，`unknown` / `not_applicable` 必须不携带值 | `execution_evidence.EvidenceField.__post_init__` | `test_execution_evidence.py`（`EvidenceFieldStateTests`） | ✅ |
| `None` 不得混同零 / 没成交 / 被拒绝：三种表示互不相同 | `execution_evidence` | `EvidenceFieldStateTests.test_known_zero_is_readable_and_distinguishable_from_unknown`、`test_unknown_and_not_applicable_are_distinguishable` | ✅ |
| 成交六分类（verified / partial / pending / none_confirmed / not_attempted / unknown）互不塌缩 | `execution_evidence.fill_verdict` | `FillVerdictTests.test_the_three_no_fill_situations_are_mutually_distinct`、`test_never_submitted_order_is_not_attempted_not_none_confirmed`、`test_in_flight_order_is_pending_not_none_confirmed` | ✅ |
| 订单写着 `filled` 但没有成交流水 → 成交数量必须 `unknown`，绝不按目标量充数 | `execution_evidence._filled_qty_field` | `FillVerdictTests.test_stored_filled_without_any_fill_row_is_unknown_not_filled` | ✅ |
| 成交必须同时有数量、可信价格、成交时段才算 verified | `execution_evidence.fill_verdict` + `execution_lifecycle.observed_fill_supported` | `FillVerdictTests.test_fill_verdict_requires_a_trustworthy_price`、`test_fill_verdict_requires_a_fill_session`、`ObservedFillSupportedTests.test_full_fill_needs_quantity_price_and_session` | ✅ |
| 卖出侧 `available_qty` 无法从历史委托重建 → 缺证据一律 `unknown`，绝不用当前持仓冒充 | `execution_evidence._available_qty_field` | `SideSpecificEvidenceTests.test_sell_available_qty_is_unknown_without_explicit_evidence`、`test_buy_available_qty_does_not_apply` | ✅ |
| 佣金只在能用权威费用模型逐分对账时才 `known`；只有合并 `fees` 字段时如实报 `unknown` | `execution_evidence.reconcile_fees` | `FeeAndSlippageTests.test_commission_is_known_when_fees_match_the_authoritative_model`、`test_sell_commission_excludes_the_stamp_tax`、`test_commission_is_unknown_when_fees_do_not_reconcile` | ✅ |
| 滑点由 `filled_price` vs `planned_price` 派生，两侧符号统一为"正=不利"；缺一即 `unknown` | `execution_evidence._slippage_field` | `FeeAndSlippageTests.test_slippage_is_adverse_positive_for_both_sides`、`test_slippage_is_unknown_without_a_planned_price` | ✅ |
| 九个状态与合法边表完备；终态不可离开 | `execution_lifecycle.ALLOWED_TRANSITIONS` | `test_execution_lifecycle.py`（`StateSetTests.test_terminal_states_cannot_be_left`、`test_every_state_has_a_transition_row_and_only_declared_targets`） | ✅ |
| 非法跳转必须失败：`CREATED -> FILLED`、`SUBMITTED -> FILLED`、`PARTIAL_FILLED -> REJECTED`、`ACCEPTED -> REJECTED` 等 17 条边 | `execution_lifecycle.can_transition` / `OrderLifecycle.advance` | `IllegalTransitionTests`（`test_every_declared_illegal_edge_is_rejected`、`test_created_cannot_reach_filled_without_submit_and_accept_evidence`、`test_submitted_cannot_reach_filled_without_accept_evidence`） | ✅ |
| `REJECTED -> FILLED` 只能通过**新的**委托生命周期表达 | `execution_lifecycle.OrderLifecycle.retry` | `RetrySemanticsTests.test_retry_creates_a_fresh_lifecycle_linked_to_the_previous_order`、`test_a_retried_lifecycle_can_fill_because_it_is_a_new_order`、`test_a_filled_order_cannot_be_retried` | ✅ |
| 部分成交不得提升为全部成交；`FILLED` 要求 `filled == requested` | `execution_lifecycle._validate_quantities` | `QuantityInvariantTests.test_filled_rejects_a_partial_quantity`、`test_partial_filled_rejects_a_full_quantity`、`ObservedFillSupportedTests.test_partial_fill_requires_a_strictly_partial_quantity` | ✅ |
| 撤单/过期可携带部分成交，但不能携带完整成交（那属于 `FILLED`） | `execution_lifecycle._validate_quantities` | `QuantityInvariantTests.test_cancelled_may_carry_a_partial_fill`、`test_cancelled_cannot_carry_a_complete_fill` | ✅ |
| stored status → 权威状态唯一映射；未知字符串 `UNKNOWN`，不猜测 | `execution_lifecycle.canonical_state` | `CanonicalStateTests.test_documented_mapping` | ✅ |
| 仓库无场所受理证据：`ACCEPTED_STORED_STATUSES` 为空集，任何 stored status 不得自称受理 | `execution_lifecycle` | `CanonicalStateTests.test_no_stored_status_may_claim_acceptance` | ✅ |
| 影子记录（`shadow_q3`）映射到 `CREATED`，永远不能合法成交 | `execution_lifecycle.canonical_state` | `CanonicalStateTests.test_shadow_records_are_created_not_submitted` | ✅ |
| "订单写着成交了"必须能被证据自证；自证不了的进 `unsupported_fill_claims` 审计 | `execution_lifecycle.observed_fill_supported` / `audit_stored_rows` | `ObservedFillSupportedTests`、`AuditStoredRowsTests` | ✅ |
| `selection_executable` 与 `execution_verified` 正交，四种组合都合法且互斥 | `execution_outcome.execution_bucket` | `test_execution_outcome.py`（`ConceptSeparationTests.test_all_four_combinations_are_legal_and_mutually_exclusive`） | ✅ |
| **必须允许** `selection_executable=True` + `execution_verified=False` | `execution_outcome.link_execution_outcome` | `ConceptSeparationTests.test_selection_executable_with_unverified_execution_is_allowed` | ✅ |
| 三层收益不得互相替代：来源分别为 `market_label_value` / `executable_selection_market_label` / `realized_fill_round_trip` | `execution_outcome` | `ReturnLayeringTests.test_the_three_return_layers_are_mutually_distinct`、`test_selection_return_is_a_counterfactual_not_a_fill` | ✅ |
| 没有成交 → `execution_return` 不成立（`not_applicable`，`maybe()` 为 `None`） | `execution_outcome.realized_execution_return` | `ReturnLayeringTests.test_no_fill_means_execution_return_is_not_defined`、`test_entry_only_fill_leaves_the_execution_return_unknown` | ✅ |
| `market_label_value` **绝不**顶替执行收益；顶替即抛 `MarketLabelSubstitution` | `execution_outcome.assert_no_market_label_substitution` | `ReturnLayeringTests.test_the_market_label_is_never_substituted_for_the_execution_return`、`test_a_known_execution_return_requires_a_verified_execution` | ✅ |
| 两腿必须是**同一标的上互补方向**的往返：两笔同向、或 `buy X` 配 `sell Y` 一律 `execution_verified=False`（方向/标的未知同样 fail closed） | `execution_outcome._round_trip_leg_identity` | `ReturnLayeringTests.test_the_legs_must_be_a_complementary_round_trip_on_one_security`、`test_an_unknown_leg_direction_cannot_be_read_as_a_round_trip` | ✅ |
| 等量**部分**成交不得产生已知执行收益：受支持的 `fill_partial` 现场必须能被表达与审计，而不是抛异常 | `execution_outcome.realized_execution_return` | `ReturnLayeringTests.test_equal_partial_fills_do_not_yield_a_known_execution_return` | ✅ |
| 身份（账户/方向/标的）与委托不符的成交流水**不是**证据：不聚合、不据以验证成交，只作为 `fill_identity_mismatch` 上报 | `execution_evidence._fill_identity_mismatches` / `load_execution_evidence` | `LoadExecutionEvidenceTests.test_a_fill_row_that_does_not_match_the_order_is_not_evidence`、`test_a_matching_fill_row_still_verifies_the_order`、`test_identity_is_not_compared_when_the_caller_did_not_check_it`、`test_a_fill_row_without_identity_columns_is_not_a_mismatch` | ✅ |
| 执行收益由真实成交往返净额计算；数量不等/费用缺失一律 `unknown` | `execution_outcome.realized_execution_return` | `ReturnLayeringTests.test_execution_return_is_computed_from_real_fills`、`test_execution_return_refuses_a_non_clean_round_trip`、`test_execution_return_is_unknown_when_fee_evidence_is_missing` | ✅ |
| PR149 selection outcome 契约字段与 `outcome_bucket` 权威分类**不被本层改动** | `selection_tradability` / `execution_outcome` | `Pr149ContractPreservationTests`（4 用例，含 `test_module_does_not_reimplement_the_tradability_bucket_priority`） | ✅ |
| 只读集成：从真实 `paper_orders` + `paper_fills` 读证据，且所选列必须存在于生产 DDL | `execution_evidence.load_execution_evidence` | `LoadExecutionEvidenceTests`（含 `test_selected_columns_exist_in_the_production_schema` 直接解析 `paper_trading.py` 的 DDL） | ✅ |
| 新模块纯 stdlib、只读、不导入 `paper_trading`；不触碰 orders/fills 之外的表 | `execution_evidence` / `execution_lifecycle` | `test_execution_evidence.py`（`ArchitectureGuardTests`）、`test_execution_lifecycle.py`（`ArchitectureGuardTests`） | ✅ |
| 状态机与证据层结论一致：无成交证据的 `filled` 行不能走到 `FILLED` | `execution_lifecycle` + `execution_evidence` | `test_execution_outcome.py`（`LifecycleBridgeTests`） | ✅ |
| 真实源码变异 M51–M60 全部被测试捕获（10/10 caught，Undetected: 0）；哨兵 S0（仅改注释）必须 UNDETECTED 以证明矩阵非空转 | `execution_evidence` / `execution_lifecycle` / `execution_outcome` | `work/execution_reality_mutation_check.py`（逐项注入、每轮清字节码缓存、逐字节 + sha256 还原核验；含基线 green 前置检查） | ✅ |

变异明细：

| ID | 注入的缺陷 | 结果 |
| --- | --- | --- |
| M51 | missing fill => assume filled（`fill_verdict` 把未知成交判成 verified） | CAUGHT |
| M52 | reject => filled（`canonical_state` 把 `risk_rejected` 映射成 `FILLED`） | CAUGHT |
| M53 | partial fill => full fill（`FILLED` 的数量不变式放宽为 `filled > requested`） | CAUGHT |
| M54 | execution return fallback market return（无成交时回落到 `market_field`） | CAUGHT |
| M55 | illegal lifecycle transition accepted（`CREATED` 的合法边集合加入 `FILLED`） | CAUGHT |
| M56 | round trip leg identity not checked（两腿身份/方向校验被拿掉，两笔买入也会被认成往返） | CAUGHT |
| M57 | equal partial fills treated as a clean round trip（等量部分成交被当成干净往返并算出已知收益） | CAUGHT |
| M58 | mismatched fill rows still aggregated（身份不符的流水只上报、仍被聚合为成交证据） | CAUGHT |
| M59 | fill identity columns not selected（loader 退回只按 `order_id` 关联，不读身份列） | CAUGHT |
| M60 | identity check self-attested without identity rows（省略身份行也自称核对过） | CAUGHT |
| S0 | 哨兵：只改注释 | UNDETECTED（预期） |

## R32 Final（COMPLETE）

| 契约 | 断言 | 测试 | mutation |
| --- | --- | --- | --- |
| shadow → paper 只消费一份 exact `ShadowComparisonReport`（`report_id == report_fingerprint`） | `AVAILABLE` + coverage 完整 + blocking 为空 + owner provenance 完整 + challenger id/version/checksum 相等 + 共享环境 EQUAL → eligible；决策指纹记录 report/scope/shadow-run 三个 exact 指纹 | `test_r32_final_promotion.py`（R-F1、R-F1b） | M-F3 / M-F4 / M-F5 |
| `PARTIAL` / `UNAVAILABLE` 永远阻断 | blocked reason 稳定（`shadow_comparison_partial` / `_unavailable` / `_blocking_reasons` / `_coverage_incomplete` / `_provenance_incomplete` / `_environment_mismatch` / `_corrupt`）；`PARTIAL` 绝不进 eligible | `test_r32_final_promotion.py`（R-F2、R-F3、R-F3b、R-F3c、R-F4c、R-F4d） | M-F1 / M-F2 / M-F9 / M-F10 |
| 无 latest / current 兜底 | repository 无 latest getter；promotion 源码无 latest 调用；被阻断的 named report 不会回落到可用的另一份；未知 id 保持 not_found | `test_r32_final_promotion.py`（R-F5、R-F5b） | M-F5 |
| proposal 生命周期 | apply → 恰好一条 `promotion` 事件（`version_created` + promotion）；AI 不可 apply；世界漂移后 proposal 变 stale 且 fail closed；`paper → production_sim` 仍 blocked | `test_r32_final_promotion.py`（R-F6、R-F6b、R-F6c） | M-F6 / M-F7 |
| 两条 authority 互不越权 | promotion 源码不出现 `strategy_champion` / `activate_params_candidate` / `self_evolution` / 指标词表；champion 源码不出现 `strategy_lifecycle` / `shadow_comparison` / `promotion_proposal`；lifecycle 只经 `SL.transition` | `test_r32_final_promotion.py`（PromotionAuthoritySeparationTests） | M-F11 / M-F12 |
| Workspace 只组装 owner 事实 | active/challenger exact identity、named report、availability/coverage/blocking 原样、readiness 等于 `strategy_promotion.evaluate` 的同一 decision fingerprint；两条 readiness 分列且不合并；无名证据保持 absent（不是 0） | `test_r32_final_promotion.py`（W1–W7） | — |
| Entry / sizing / observation 边界冻结 | Active 黄灯 per-strategy `risk_scale`（0.5/0.75/0.65）与红灯 sector-heat `shadow_exception` 语义不变；`EntryGateState` 与 planner 均无 sizing/observation 字段；manual 用的 canonical gate 无 `risk_scale`；比对报告不含 Active sizing modifier | `test_r32_final_ownership_boundary.py`（R-F9、R-F9b、R-F10、R-F11、R-F12） | — |
| 前端不重算 readiness、不把缺失当 0 | AVAILABLE/PARTIAL/UNAVAILABLE 原样渲染；缺失渲染为「不可用」而非 `0`/`0/0`；两条晋级链分别命名且不合成；渲染函数内无 coverage→ready 推导 | `frontend/tests/challenger-workspace.test.mjs`（W8–W16） | M-F13 |

## R33-A Exact Strategy Health Evidence（COMPLETE，PR #222 MERGED）

| 契约 | 断言 | 测试 | mutation |
| --- | --- | --- | --- |
| 同一组 exact 输入 → 同一快照指纹 | 重复采集得到同一 `snapshot_id`；指纹覆盖 exact version + 显式窗口 + 维度来源指纹；**不含** `created_at` | `test_r33a_strategy_health.py`（H1、H1b、H1c、H4b） | M-H3 / M-H8 |
| 身份与窗口 fail closed | exact version 不存在或 checksum 不符 → `exact_strategy_version_not_persisted`；窗口缺失/非法/非前向 → 稳定 reason | H2、H3、H4 | M-H1 / M-H2 |
| 维度只报告 owner 事实 | runtime 用 registry 的 exact-version readiness；lifecycle 用 exact state + 事件指纹；execution 用 owner 的核验结论列与唯一谓词；risk 按 `decision_provenance.authority` 分档；comparison 只认显式 report id | H5、H5b、H6、H6b、H7、H7b | M-H4 / M-H5 |
| 缺失保持缺失 | performance 无 owner → `strategy_performance_owner_unavailable`；signals 无法归属到 version → `signal_strategy_attribution_unavailable`；未盖章的订单记 `not_stamped`，不升级成 verified、也不折算成 unknown | H7、H8、H9 | M-H4 / M-H6 |
| coverage 只是证据覆盖率 | 分母恒为全部维度；`ratio == available/expected`；PARTIAL/NOT_APPLICABLE 不算覆盖；缺维度需带 reason | H10、H10b | — |
| 历史不愈合 | 快照按 id 精确读取，新证据出现后旧快照字节不变；新事实需要新快照 | H11、H15 | M-H7 |
| append-only 与 exact get | 同 id 幂等；同 id 不同内容冲突；坏 id 形状拒绝；非空表上未知 id 不得兜底到最新；只有 `append_snapshot`/`get_snapshot` | H12、H13、H14、H15 | M-H7 / M-H10 |
| 零 lifecycle / 零正式账本写入 | 采集前后 lifecycle state + 事件序列、以及 orders/fills/positions/nav 计数完全一致 | H16、H17 | M-H9 |
| 纯 builder 无 ambient 依赖 | 无 sqlite3 / 网络 / 时钟 / provider；网络与时钟被 poison 后仍确定性 | H18 | — |
| 最小 API surface | `POST …/health/snapshots` + `GET …/health/snapshots/{id}`；无 health/latest；身份冲突 400、未知快照 404 | `StrategyHealthApiSurfaceTests` | — |

## R33-B Strategy Retirement Policy（COMPLETE，PR #223 MERGED）

| 契约 | 断言 | 测试 | mutation |
| --- | --- | --- | --- |
| 同一快照 → 同一决策；policy version 与快照身份都进指纹 | 重复评估同一快照得到同一 `decision_id`；不同快照/不同 policy version ⇒ 不同决策；内容相同但身份不同的两份快照必须得到不同决策 | `test_r33b_retirement_policy.py`（P1、P2、P3、身份参与指纹） | M-R1 / M-R6 |
| 证据完整性闸门最先 | 任一维 PARTIAL/UNAVAILABLE ⇒ `INSUFFICIENT_EVIDENCE`（逐维点名），绝不产出动作；真实快照（performance UNAVAILABLE + activity PARTIAL）必然命中 | P4、P5、P5b、P6、P7 | M-R2 / M-R3 |
| UNKNOWN / PARTIAL 永不触发退休 | `risk_evidence` UNAVAILABLE 既不判「好」也不判「坏」；`performance` UNAVAILABLE 不是「收益 0」；`execution` PARTIAL 不得 retire | P5、P6、P7 | M-R2 / M-R3 |
| 0 活动不是退休信号 | 窗口内 0 笔订单仍不得产出退休候选；证据完整 + 无信号 ⇒ `NO_ACTION` | P8、NO_ACTION 用例 | — |
| policy 不读原始表 / 不写 lifecycle | AST 判定：policy 只 import 事实层（无 sqlite3/paper_trading/strategy_lifecycle）；不存在任何名为 `transition` 的调用；评估期间 lifecycle state 与事件序列字节不变 | P9、P10 | M-R5 |
| 快照身份不符即 fail closed | 被篡改（指纹自校验失败）的快照不得作为决策输入 | P13 | M-R7 |
| append-only 与 exact get | 同 id 幂等；同 id 不同内容冲突；表拒绝 UPDATE/DELETE；未知 id 不得兜底到最新；只有 `append_decision`/`get_decision` | P11、P12、P14、append-only 触发器用例 | M-R4 / M-R8 |
| proposal 只描述不执行 | 候选决策 → 固定 target_state，且必须落在 lifecycle owner 的合法边内（否则不产出）；`executed=false`；NO_ACTION / INSUFFICIENT_EVIDENCE 不产出 | TransitionProposalTests | — |
| 纯函数在 provider/AI/时钟 poison 下不变 | socket / time / datetime.now 全部 poison 后决策指纹不变 | P15 | — |
| 最小 API surface | `POST …/retirement/evaluate` + `GET …/retirement/decisions/{id}`；无 status / latest 端点；未知证据 404、身份冲突 400 | RetirementApiSurfaceTests | M-R4 |

## R33-C Strategy Retirement Controlled Workflow（IN REVIEW）

| 契约 | 断言 | 测试 | mutation |
| --- | --- | --- | --- |
| Proposal exact binding | proposal 绑定 decision/snapshot 指纹、strategy id/version/checksum、current/target state；错 snapshot、版本或 checksum fail closed | C1–C3 | M-C3 / M-C7 |
| Operator approval | 仅显式 operator 可 APPROVE/REJECT；pending 不可执行，REJECT 不写 lifecycle；审批本身不执行 | C4–C6、API validation | M-C1 / M-C2 |
| Controlled transition | 仅 APPROVED proposal 执行；执行前复核 decision、snapshot、版本、checksum、current state 与 `TRANSITION_TABLE`；经 R31 CAS/event owner 一次执行；retry 从 exact matching event 返回相同执行结果语义 | C6–C8 | M-C3 / M-C4 / M-C6 / M-C9 |
| Append-only evidence | 同一 proposal append 幂等；proposal / approval 指纹篡改可检测；表拒绝 UPDATE/DELETE；无 current/latest 字段或 fallback | C9–C11、repository schema guard | M-C5 / M-C7 / M-C8 |
| 无 AI/provider 自动决策 | approval 只接受 operator 身份；workflow 不依赖 AI/provider；health/policy 不导入 lifecycle；只有 execute 用例调用 lifecycle transition | C12、C14–C15 | M-C1 / M-C6 |
| 正式账本隔离 | execution 只产生 lifecycle event，不改 cash、positions、orders、fills | C13 | — |
| API 边界 | 创建、approve/reject、execute、exact proposal GET；无 current status 或 latest proposal endpoint | `RetirementWorkflowApiTests` | M-C5 |

阶段状态：R31 **COMPLETE**；R32 **COMPLETE**；R33-A **COMPLETE**；R33-B **COMPLETE**；R33-C **IN REVIEW**；R33 **NOT COMPLETE**。本 PR **MERGE = NOT MERGED，DEPLOY = NOT DEPLOYED**。

