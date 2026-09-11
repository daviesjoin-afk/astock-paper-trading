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

## 维护约定

1. 新增门禁必须先在本表加一行，再写测试；表格状态从 ⚠️ → ✅。
2. `⚠️` = 逻辑已实现但缺独立离线单测，是贡献者 `good first issue` 的优先候选。
3. demo 叙事用例（`test_demo_seed` / `test_demo_replay_golden`）作为引擎路径的端到端兜底回归。
