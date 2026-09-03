# Architecture notes

本项目是 A 股模拟交易系统（paper-only），核心路径应保持为：

```text
HTTP API / scheduler
        ↓
application services
        ↓
deterministic domain rules
        ↓
paper ledger / read models
```

行情提供方、SQLite 和 LLM 属于基础设施，不应被纯决策规则隐式调用。`adaptive_*` 模块是影子学习与审阅路径，只能读取行情和模拟账本，不能依赖订单执行入口。

## 当前边界

- `backend/main.py` 负责 FastAPI 组装、只读查询和生命周期。
- `backend/api_paper.py`、`backend/api_adaptive.py` 负责 HTTP 契约；`confirmed=true` 是防误触确认，不是身份认证。
- `backend/paper_trading.py` 仍是主要交易编排与账本实现，后续按小步拆出 Execution、Portfolio、Risk 和 Scheduler。
- `backend/decision_context.py` 集中定义决策证据快照与兼容加载适配器；`decision_engine.py` 的公开入口暂时保持不变。
- `backend/decision_rules.py` 承载不依赖数据源的评分、买入时机、止盈和止损规则；规则模块不联网、不读缓存、不写账本。
- `backend/marketdata_transport.py` 负责共享 HTTP 连接、重试和源熔断状态；`data_fetcher.py` 继续兼容性导出旧名称，解析与缓存逻辑暂不改变。
- `backend/marketdata_cache.py` 负责可注入的 TTL 内存缓存、全市场快照 single-flight 文件锁和数据源健康文件读写；`data_fetcher.py` 保留旧 `_cached`/锁/健康状态入口。
- `backend/marketdata_providers.py` 负责东财 `clist`、概念成员分页适配以及腾讯/新浪实时行情与 K 线响应解析；HTTP 会话、重试和可变主机健康状态由 `data_fetcher.py` 注入，确保完整性元数据和旧入口兼容。
- `backend/marketdata_normalizers.py` 承载行情行、证券代码、时间戳和 K 线 DataFrame 的无副作用标准化；`data_fetcher.py` 保留旧函数包装以兼容现有 provider 调用。
- `backend/paper_trading_rules.py` 承载交易日、费用、证券类型与证券权限等无账本副作用规则；`paper_trading.py` 继续兼容导出旧的下划线函数名。
- `backend/paper_quote_policy.py` 承载行情新鲜度、活跃度和成交核验门禁；`paper_trading.py` 保留 `_quote_is_fresh`、`_is_trading_active`、`_execution_quote_status` 兼容包装。
- `backend/paper_allocation.py` 承载共享池席位分配和策略预算的纯计算；`paper_trading.py` 只负责读取风险/持仓/预约数据并注入常量。
- `backend/paper_sizing.py` 承载按风险、权重、资金、行业和共享池约束计算下单股数；`paper_trading.py` 保留 `_price_aware_qty` 兼容包装。
- `backend/paper_storage.py` 负责 SQLite 连接生命周期、只读连接、WAL 检查点和锁重试；`paper_trading.py` 只保留兼容包装，不把业务查询迁入存储层。
- `backend/paper_portfolio.py` 负责将已读取的持仓 lot 聚合为兼容读模型；数据库查询与交易结算仍由 `paper_trading.py` 编排。
- `backend/paper_archive_projection.py` 负责把不可变历史周期快照投影为只读订单行；损坏快照隔离在投影边界内，不影响当前账本。
- `backend/paper_ledger_reader.py` 是 adaptive 读取 paper ledger 的只读端口；使用 SQLite `mode=ro` 与 `query_only`，补偿恢复等明确写路径不经过该端口。
- `backend/strategy_registry.py` 集中策略 ID、展示名称和 active/legacy 状态；adaptive、adaptive risk、research、selection 和 strategy-center 展示从这里读取，暂不改变交易调度或账户范围。
- `backend/paper_repository.py` 提供通用 ledger 行读取、审计写入、dashboard 账户批量投影和活动订单轻量投影；`paper_trading.py` 保留旧 `_rows`/`_audit`/`_account_metric_inputs` 包装，后续再迁移对象级 SQL。
- `backend/paper_performance.py` 负责今日报价新鲜度、持仓今日盈亏和卖出贡献的纯计算；`paper_trading.py` 保留 `_today_*` 兼容包装。
- `backend/paper_schema_migrations.py` 集中 paper ledger 的增量字段、运行时租约字段和点火影子表迁移；`db_migrate.py` 通过版本号调用这些幂等操作，应用前使用 SQLite backup API 创建一致性副本，运行引擎不再内联 `ALTER TABLE`。
- `backend/adaptive_risk.py` 使用 outbox（跨数据库操作意图表）保证纸盘提交后，adaptive 账本可重放收敛。
- `backend/test_adaptive_dependency_boundary.py` 以 AST（源码语法树）守护 adaptive 不直接导入纸盘订单 API，也不出现订单提交/取消调用。
- `backend/adaptive_genetics.py` 承载 alpha 实验室的基因归一化、交叉、变异和适应度纯计算；`adaptive_engine.py` 保留数据集/训练编排和兼容包装。
- `backend/adaptive_shadow_risk.py` 承载 adaptive 组合影子风控的历史归一化、波动率、集中度和压力测试纯计算；不读账本、不联网、不提交订单。
- `.github/workflows/ci.yml` 覆盖后端测试、编译/前端语法与镜像一致性，并新增独立 Docker 构建和健康端点冒烟检查。
- `frontend/app.js` 是前端运行时的 canonical source（唯一源文件）；`frontend/assets/` 在 Docker 构建时同步生成。

## 不变量

1. 不连接真实券商、不产生真实订单。
2. 证据缺失、行情过期或跨源核验失败时，决策 fail-closed。
3. adaptive 自动流程只能 shadow-only；风控放宽必须人工确认。
4. 迁移和跨账本恢复必须可重复、可审计、可回滚。
5. 任何拆分必须保持公开 API、审计事件和既有交易规则兼容。

## 目标依赖方向

```text
API → Service → Domain
                 ↑
        Infrastructure adapters
```

Domain 不直接依赖 FastAPI、SQLite、Eastmoney、Tencent、Sina 或具体 LLM SDK。这个目标会通过渐进拆分实现，不做一次性重写。

## 本次变更

本次先同步服务器工作区并修复三个可验证边界：前端缓存版本/运行时镜像一致性、活动页风控审计请求上限，以及风险 outbox 在纸盘已提交后的 adaptive 侧重放收敛。随后新增 `decision_context.py` 集中证据读取、`decision_rules.py` 承载纯规则、`marketdata_transport.py` 承载 HTTP 传输、`marketdata_cache.py` 承载缓存与快照锁、`marketdata_providers.py` 承载东财分页与概念成员适配及腾讯/新浪实时与 K 线响应解析、`marketdata_normalizers.py` 承载行情标准化、`paper_trading_rules.py` 承载交易日/费用/证券权限规则、`paper_quote_policy.py` 承载行情新鲜度/活跃度/成交核验门禁、`paper_allocation.py` 承载共享池席位与策略预算纯计算、`paper_sizing.py` 承载下单股数纯计算、`paper_storage.py` 隔离 SQLite 连接生命周期、`paper_portfolio.py` 承载持仓 lot 聚合、`paper_archive_projection.py` 承载历史归档订单只读投影、`paper_performance.py` 承载今日盈亏纯计算、`adaptive_genetics.py` 承载 adaptive alpha 纯遗传计算、`adaptive_shadow_risk.py` 承载影子组合风控纯计算、`paper_ledger_reader.py` 为 adaptive 提供只读 paper ledger 端口、`strategy_registry.py` 集中策略身份口径，并以 `paper_repository.py` 建立 ledger 通用仓储薄接口和 dashboard 账户批量投影；本阶段再以 `paper_schema_migrations.py` 集中增量 schema 变更，`db_migrate.py` 提供事务化、可回滚的 v1-v4 迁移入口。兼容导出和回归测试守住现有行为。更大范围的对象级仓储、provider 分层和模块拆分仍按 `docs/PRD-architecture-hardening.md` 分阶段推进。
