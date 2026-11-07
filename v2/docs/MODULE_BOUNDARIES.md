# V2 模块边界：轻量、单向、可审计

本阶段刻意**保持单个 Cargo crate 和一个 Rust 进程**。只有当某个领域确实需要独立发布、共享或独立部署时才评估拆 Cargo workspace；不为了漂亮的目录结构新增服务、crate、facade 或抽象工厂。

| 模块 | 唯一职责 | 可依赖 | 禁止 |
| --- | --- | --- | --- |
| `src/domain/contract.rs` | 强类型实验/策略身份、请求、报价证据及引用、成交、规则版本 | serde、标准库 | IO、SQLite、HTTP、V1 |
| `src/domain/portfolio.rs` | 现金/持仓 lot、T+1 状态转移 | contract | IO、SQLite、HTTP、V1 |
| `src/domain/calendar.rs` | 固定日历版本下日期是否计划开市、覆盖范围与来源 | contract、chrono、serde、标准库 | IO、系统时钟、网络、成交数量、V1 |
| `src/domain/instrument.rs` | 固定离线证券身份及适用日期下受支持的数量规则 | contract、chrono、serde、标准库 | IO、时钟、网络、价格、持仓、V1 |
| `src/domain/execution.rs` | 精确时段策略、资格与证据有效性、决策、手续费 | contract、calendar、instrument、portfolio、chrono | IO、SQLite、HTTP、V1 |
| `src/store/mod.rs` | 单 SQLite 写入口、实验初始资金及固定引用、schema、原子提交、幂等 | domain、rusqlite | HTTP、Python、V1 schema |
| `src/store/query.rs` | 只读投影、历史回放 | store、domain | SQL 写入、提交 |
| `src/main.rs` | localhost HTTP 路由、请求响应转换 | store、domain、axum | 交易规则复制、直接 SQL |
| `research/worker.py` | 产出可复现的研究信号 | Python 标准库和 API 契约 | 下单数量、账本直写、旧框架 import |
| `web/src/main.ts` | 只读交互和可视化 | V2 HTTP API | 业务撮合、仓位写入、V1 接口 |

依赖方向固定：**HTTP → Store → Domain**。研究通过版本化 JSON 调用 HTTP，浏览器读取投影。Domain 没有任何运行时副作用；只有 Store 具有账本写权限。

## 不变量（具体规则而非文件大小）

1. `Signal` 不接受 `quantity`、`shares`、`amount` 等未知字段；`Quote` 未知字段也必须拒绝，而非默默忽略。
2. 不隐式读取当前时间、当前行情、当前策略；显式传入时点、证据、周期和版本。
3. 一个请求身份的重试不会再次写成交；不同负载复用同一身份必须冲突。
4. 同一模拟请求的决策和 Fill 在一个 SQLite 事务落库；回放复核严格一致。
5. 交易执行只有一处生产写入路径；研究侧无法拿到数据库连接。
6. V2 从不读取/迁移 V1 运行库，不接券商，不触发自动激活。
7. 契约 v2 的研究请求只引用 `quote_ref`，不接受内嵌报价。Store 独占证据入库和按身份解析；Domain 只接收显式证据，校验其可见时点。
8. 每次决策固定实际使用的证据快照（包括缺失）；回放不得用后补报价改写拒绝。现有证据入库内容必须与已固定快照一致。详见 [QUOTE_EVIDENCE.md](QUOTE_EVIDENCE.md)。
9. `QuoteImport` 是不可信离线导入命令；`QuoteEvidence` 是 Store 生成的持久化事实。只有 Store 记录 `received_at`，Domain 不读时钟。离线证据固定为 `offline_fixture`，供应商发布时间未知，不存在可自报的认证等级。详见 [SOURCE_TRUST.md](SOURCE_TRUST.md)。
10. Store 在现有 meta 表固定日历/时段完整快照，每条正常或拒绝决策记录版本引用；回放只读这些快照并核对引用。资料或代码策略升级使用新版本，旧快照不匹配时拒绝，不用后来资料重算。详见 [CALENDAR_SESSIONS.md](CALENDAR_SESSIONS.md)。
11. 参考价不能直接成交。execution 只使用同报价身份、同方向且新鲜的执行证据价格；候选量超过显式容量就整笔拒绝。Store固定执行块且不可覆盖，不新增容量余额权威。详见 [EXECUTION_EVIDENCE.md](EXECUTION_EVIDENCE.md)。
12. 六位代码只代表格式，不能推导板块/类型。instrument独占固定身份及数量规则适用事实，消费者为execution；Store固定完整离线快照、每份证据的解析身份及run版本，query仅只读核对。未知/冲突/未实现类型与规则、覆盖外日期拒绝。研究和普通导入不能自报资格或覆盖快照。详见 [INSTRUMENT_ELIGIBILITY.md](INSTRUMENT_ELIGIBILITY.md)。

**不做的事情**：不设计通用事件总线、不做微服务、不接 Kafka/Redis/ORM、不增加“为了模块化而存在”的 facade 或重复数据模型、不使用硬编码 LOC/函数行数阈值作为合并门禁。

## 进一步拆分的门槛

只有出现以下可证明原因才拆新模块或 crate：独立业务事实所有权、可观察的循环依赖、不同的持久化事务生命周期、跨进程安全边界、独立可复用 API。出现单纯的大文件或行数增长，不自动触发拆分；优先审查函数是否承载了多种不同业务职责。

## 独立实验（ADR 001）

cycle_id 是唯一资金归属，不是调度周期。创建后固定初始资金、策略研究身份、数据包和执行规则，完成后拒绝新决策。写入、时间顺序、状态重建、历史查询、计数与回放全部按实验隔离。SQLite schema 6、信号契约 3；规则版本保持 6。当前只开放 cn-offline-fixture-pack-1，多个受审核数据包的登记扩展边界见 [ADR 001](ADR-001-EXPERIMENT-OWNERSHIP.md)。

## 独立数据包（ADR 002）

Store 仍唯一登记固定清单及写入证据；query 按实验固定的包读取和校验。contract 定义不可变清单/完整上下文及显式带包的报价身份；calendar/instrument/execution 只消费传入快照。schema 7 一次增加包表、实验清单引用及证据 pack_id 主键；执行策略仍为 6。只允许明确审核的两个 offline_fixture 包，不提供任意上传。后续包增加记录和审核定义，不改变交易表。详见 [ADR 002](ADR-002-MARKET-DATA-PACKS.md)，其版本说明取代上文之前阶段的单包限制。
