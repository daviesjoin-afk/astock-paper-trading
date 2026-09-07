# 撮合与数据门禁场景矩阵

对应 issue #27 / #23：一眼看出每个关键交易门禁的覆盖与缺口。
约定：**正向** = 允许成交的用例；**拒绝** = 必须拦下的用例。全部测试离线可跑。

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

## 维护约定

1. 新增门禁必须先在本表加一行，再写测试；表格状态从 ⚠️ → ✅。
2. `⚠️` = 逻辑已实现但缺独立离线单测，是贡献者 `good first issue` 的优先候选。
3. demo 叙事用例（`test_demo_seed` / `test_demo_replay_golden`）作为引擎路径的端到端兜底回归。
