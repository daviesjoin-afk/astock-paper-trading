# R32-C：隔离式 Shadow Runtime

状态：R32-C IN REVIEW。本文记录生产路径盘点与本阶段契约；R32-D 的比较报告、晋级判断、UI 均不在范围内。

## 开始实现前的生产路径盘点

1. **Active 冻结的外部环境在哪里形成**：当前逐笔执行的 Active facts 在 `execution_planner.execution_context_from_facts()` 形成。它消费调用方已取得的 quote，并在一个显式 execution-as-of 上构造 market reading、读取 tradability archive、捕获正式执行状态。`_runtime_context_for_order()` 随后把 exact cycle strategy pin 与市场、quote、tradability、执行状态指纹绑定进 `ComparableRuntimeContext`。该上下文是某次逐笔决策的证据，不是一个独立、可供 Shadow 查询的市场服务。
2. **共享环境字段**：R32-B 的市场政策名、market snapshot fingerprint、symbol quote fingerprints、tradability fingerprints、session date、decision instant、execution ruleset 属于外部环境。DSL 实际消费的每个 symbol factor snapshot 也有独立 fingerprint 并纳入 environment identity。Active 的 strategy ID/version/checksum、risk profile/state、entry gate state/policy、execution quantities/cash 属于各自策略腿，不得放进共享环境身份。
3. **策略专属状态**：strategy immutable stamp、lifecycle、risk policy/state、entry policy/state、reference positions/cash、pending intents 由各自的运行腿显式提供。不能通过 `ComparableRuntimeContext.context_fingerprint` 判断两条策略腿是否共享环境。
4. **已有纯决策边界**：声明式策略规则由 `strategy_dsl_evaluator.evaluate()` 纯函数执行；Signal Authority 的纯决定封装是 `signal_service.decide_signal()`；entry gate 是 `execution_planner.evaluate_entry_state()`；模拟撮合是同模块 `evaluate_simulated_execution()`。本实现调用这些 owner，不复制其条件、费用、滑点、手数、T+1、流动性参与率或风险规则。
5. **有 DB 副作用的生产适配器**：`execution_planner.execution_context_from_facts()` 会读 `TradabilityArchiveRepository` 与正式 orders/reservations/lots 状态；`plan_entry()` 会读正式账户和容量/现金；`signal_service.commit_signal()` 写正式 `paper_signals`；`execution_planner.commit_fill()` 写正式 orders/fills/cash/lots/reservations 与风险审计；`strategy_lifecycle.transition()` 写 lifecycle authority。Shadow 不调用这些正式写入/当前状态适配器，也不拥有生命周期转换权。
6. **可复用的现有 authority**：Strategy Registry 通过 `get_version(strategy_id, version, checksum=...)` 提供 exact immutable version；R31 Lifecycle 通过 `get_state()` 提供该 exact version 状态；DSL、Signal decision、Entry gate、Execution simulation 复用上述纯 boundary；Market Data Contract 与 Tradability Archive 的 owner-issued typed evidence 由 Active capture 提供，Shadow 只核对并消费，不查询 provider、latest cache 或 archive。

## 业务所有权与输入边界

- ShadowRun append-only evidence 是 Shadow 执行事实的唯一 owner。派生展示数据必须从 run evidence 重建，不能反向成为事实来源。
- `ComparableEnvironmentIdentity` 只证明两条策略腿共享同一外部输入。规范 JSON 排序后 SHA-256；不含策略身份、策略风险/持仓/资金状态，也不依赖进程 hash、repr、机器时间或随机 ID。
- 本阶段的 `FrozenShadowEnvironment` 必须带 R32-B `ComparableRuntimeContext` 作为 Active capture 来源凭证；共享环境各 identity 字段必须与该 context 相符，active comparator exact stamp 也必须相符。tradability 输入必须是 Tradability Archive owner 的不可变 `TradabilityDecision` 类型，并与 code/session/decision instant/fingerprint 精确匹配；模块可以引用这个值契约，但绝不读取 `TradabilityArchiveRepository`。factor snapshots 是策略 DSL 实际消费的额外冻结输入，按内容生成 fingerprint；所有输入随 ShadowRun 保存。Shadow runner 不允许 provider、行情 cache、最新 tradability/market 查询。缺一个身份或身份不一致都返回不可比/不可运行。
- 每次运行必须显式给出 challenger 与 active comparator 的 exact `(strategy_id, version, checksum)`、共享环境 fingerprint、精确 session/decision instant 和正数 reference capital。reference capital 是模拟基准，不读取正式 NAV/cash/allocation。
- challenger exact version 的 R31 state 必须为 `shadow`；本模块没有 lifecycle transition API。版本缺失、checksum 不符、非 Shadow 状态、环境不一致或 continuation 链缺失都 fail closed。
- continuation 必须显式引用 `previous_shadow_run_id`，并以那条不可变 run 的 after-state 作为 before-state。禁止查询“最新 run”。初次运行明确传 `None`。
- Shadow reference state 只保留现金、持仓/可卖数量、同 session 已成交消费量和 turnover 所需数据，不复刻正式账户、lot 或订单表。Entry Authority 复用 `EntryGateState` 纯评估，但会先用该 Shadow 链的 reference cash、Shadow positions、空的正式预占/共享池状态构造隔离快照；调用者传入的 formal cash/positions/reservations 不会进入 Challenger 决策。
- 允许的持久写入仅为正式 migration 建立的 ShadowRun append-only evidence。正式账户现金、持仓、lots、orders、fills、reservations、risk/capacity 状态在 Shadow run 前后不得变化。

## 纯逻辑与适配边界

- `shadow_run_service.run_shadow()` 是生产应用入口，只按 spec 中 exact stamp 读取 Registry 与 R31 Lifecycle，并按显式 prior ID 读取 continuation；它不查 current head/latest/provider。纯 `shadow_runtime.evaluate_shadow()` 接收 owner-resolved immutable spec、FrozenShadowEnvironment、exact definition 和已捕获 entry/risk inputs，不打开数据库、不取当前时间、不加载 provider。
- repository 只负责按稳定 run identity 幂等追加完整输入/输出 evidence，并按显式 ID 读取 continuation。运行决策不依据 repository 的 latest 查询。
- migration 负责建表和 append-only guards；业务代码不执行运行时 CREATE/ALTER。
- 对目前只有正式账户语义或没有可移植的 pure boundary 的 native strategy 路径，保持 unavailable；本阶段先支持有 exact immutable DSL 的 Challenger，不复制原生策略实现。

## R32-D 前置条件

需要一个可审计的 Active capture producer，能对目标 session/decision instant 一次性输出覆盖 Active 所需 symbols 的 frozen external evidence；每一份 ShadowRun 保存同一个环境身份及两条策略腿的 exact stamps。R32-D 才能基于 exact Active evidence ID 与 ShadowRun ID 生成 comparison coverage/deltas。R32-C 不生成 winner、总分、晋级建议或 comparison report。
