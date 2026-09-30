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
- 本阶段的 `FrozenShadowEnvironment` 必须带 R32-B `ComparableRuntimeContext` 作为 Active capture 来源凭证；共享环境七个维度（session date、decision instant、market policy、market snapshot fingerprint、quote fingerprints、tradability fingerprints、execution ruleset identity）必须逐项与该 context 相符——比较按维度整体进行，缺少任一维度即视为不同——active comparator exact stamp 也必须相符。tradability 输入必须是 Tradability Archive owner 的不可变 `TradabilityDecision` 类型，并与 code/session/decision instant/fingerprint 精确匹配；模块可以引用这个值契约，但绝不读取 `TradabilityArchiveRepository`。factor snapshots 是策略 DSL 实际消费的额外冻结输入，按内容生成 fingerprint；所有输入随 ShadowRun 保存。Shadow runner 不允许 provider、行情 cache、最新 tradability/market 查询。缺一个身份或身份不一致都返回不可比/不可运行。
- 每次运行必须显式给出 challenger 与 active comparator 的 exact `(strategy_id, version, checksum)`、共享环境 fingerprint、精确 session/decision instant 和正数 reference capital。reference capital 是模拟基准，不读取正式 NAV/cash/allocation。
- challenger exact version 的 R31 state 必须为 `shadow`；本模块没有 lifecycle transition API。版本缺失、checksum 不符、非 Shadow 状态、环境不一致或 continuation 链缺失都 fail closed。
- continuation 必须显式引用 `previous_shadow_run_id`，并以那条不可变 run 的 after-state 作为 before-state。禁止查询“最新 run”。初次运行明确传 `None`。
- Shadow reference state 只保留现金、持仓/可卖数量、同 session 已成交消费量和 turnover 所需数据，不复刻正式账户、lot 或订单表。Entry Authority 复用 `EntryGateState` 纯评估，但会先用该 Shadow 链的 reference cash、Shadow positions、空的正式预占/共享池状态构造隔离快照；调用者传入的 formal cash/positions/reservations 不会进入 Challenger 决策。
- 允许的持久写入仅为正式 migration 建立的 ShadowRun append-only evidence。正式账户现金、持仓、lots、orders、fills、reservations、risk/capacity 状态在 Shadow run 前后不得变化。DDL 的单一事实来源是 `paper_schema_migrations.ensure_shadow_runs_table`：migration v24 与 `init_db` 的两条路径（既有账本快路径、新建库路径）调用同一个函数，既有账本升级后不会缺表。
- 每份 run evidence 保存本次运行**消费的输入**：每个 candidate 的 requested quantity、reference price、order type、捕获的 `EntryGateState` 全字段（含调用方捕获的 `account_risk_state`/`market_state`）与声明的 risk policy identity；因此旧 run 的输入不需要重新查询当前世界即可审计。同一 run 内 `(symbol, side)` 重复的 candidate 直接拒绝——重复腿没有自身确定顺序，不能靠调用方顺序决定 run identity。
- entry policy 仍由现有 owner（`execution_planner`）拥有，R32-C 不新增 shadow policy 表，也不复制 chase lane / 席位预留 / 手动复核 / 红灯文案判据。策略表只在应用边界解析一次：`shadow_run_service.run_shadow()` 在进入纯运行时前调用 `execution_planner.execution_policy_snapshot(challenger_strategy_id)`，把当时的策略冻结成不可变 `ExecutionPolicySnapshot`（绑定身份 + owner 字段投影 + fingerprint），并把同一份对象显式传入 `evaluate_shadow()`。纯运行时与 `evaluate_entry_state(execution_policy=...)` 只消费该快照，不再解析当前 owner 状态；因此 replay 不会随策略表漂移。
- 冻结策略的身份以 run-level `challenger_runtime_inputs.entry_policy` 保存一次（不按 candidate 重复）：`fingerprint`、绑定 `account_id`、策略自身行 `policy_account_id`、chase lane、手动复核、席位预留 owner/持有/期限、红灯文案与 execution planner version。字段集由 execution_planner 的投影助手提供，Shadow 不另立一份容易漂移的字段表。未知/custom challenger 沿用保守默认策略时，绑定身份是 challenger id 而 `policy_account_id` 为空——不因默认策略自身的空 identity 拒绝绑定。
- `runtime_context.entry_policy_fingerprint` 与显式传入的冻结策略 fingerprint 不一致时 fail closed；不会重新解析当前策略来自动纠正指纹，也不会在显式传入后回退到当前策略表。
- 捕获的 `account_risk_state` 与 `market_state` 是调用方显式提供的运行输入证据，不属于共享环境身份，也不回写正式风控状态。挑战者自己的 `position_limit` 仍然生效：Shadow 只忽略正式组合的占位与共享池预留，不取消该上限。

## 纯逻辑与适配边界

- `shadow_run_service.run_shadow()` 是生产应用入口，只按 spec 中 exact stamp 读取 Registry 与 R31 Lifecycle，并按显式 prior ID 读取 continuation；它不查 current head/latest/provider。它同时是 Entry `ExecutionPolicy` 的唯一捕获点：捕获一次并冻结后再进入纯运行时。纯 `shadow_runtime.evaluate_shadow()` 接收 owner-resolved immutable spec、FrozenShadowEnvironment、exact definition、已冻结的 entry policy 以及已捕获 entry/risk inputs，不打开数据库、不取当前时间、不加载 provider、不解析当前策略表。
- repository 只负责按稳定 run identity 幂等追加完整输入/输出 evidence，并按显式 ID 读取 continuation。运行决策不依据 repository 的 latest 查询。
- migration 负责建表和 append-only guards；业务代码不执行运行时 CREATE/ALTER。
- 对目前只有正式账户语义或没有可移植的 pure boundary 的 native strategy 路径，保持 unavailable；本阶段先支持有 exact immutable DSL 的 Challenger，不复制原生策略实现。

## R32-D 前置条件

需要一个可审计的 Active capture producer，能对目标 session/decision instant 一次性输出覆盖 Active 所需 symbols 的 frozen external evidence；每一份 ShadowRun 保存同一个环境身份及两条策略腿的 exact stamps 与本次运行消费的 candidate 输入。R32-D 才能基于 exact Active evidence ID 与 ShadowRun ID 生成 comparison coverage/deltas。R32-C 不生成 winner、总分、晋级建议或 comparison report，也不提供生产调用者（runtime wiring 属 R32-E）。

术语边界（R32-D/E 必须保持）：entry policy 在本阶段已是 **owner-issued frozen input**——它由 `execution_planner` 捕获并冻结，可称为 owner projection。`risk_policy_identity` 仍只是 **caller-declared / captured input**：R32-C 只把它当作运行证据保存，没有额外的 owner provenance 校验。因此 R32-D/E 在把它写进任何 promotion evidence 之前，必须先解决这个区别（补 provenance 或明确标注为 declared），不得直接称其为 owner-verified risk policy identity。
