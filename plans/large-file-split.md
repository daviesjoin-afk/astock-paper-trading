# 大文件职责拆分

## 完成标准

在前轮瘦身结果上逐个拆分可独立验证的职责，不改变交易规则、事实身份、数据库结构和接口行为。第一批从 `paper_trading.py` 提取任务事实契约与只读读取器，研究侧直接依赖新模块，旧入口保留同身份兼容导出。

## 第一批范围

- `paper_trading.py` → `paper_runtime_facts.py`：状态词表、不可变任务事实、时间/内容校验、按明确时点读取与分页。
- `ai_research_runtime_adapter.py`、`deepseek_research.py`：直接读取新模块，避免研究事实读取加载交易主文件。
- 现有运行事实、分页、研究汇聚检查与故障注入：跟踪实际实现位置，保留所有拒绝条件。
- 原有瘦身改动继续保留；本批不同时拆选股流程或成交编排。

## 阶段

- [x] 核对真实依赖、兼容导出、测试与故障注入目标。
- [x] 原样提取实现，调整研究调用方和检查目标。
- [x] 验证独立导入、类型/函数身份、实现等值、错误拒绝、分页及故障注入。
- [x] 运行全量后端检查，记录结果并更新拆分状态。

## 必须保持

- 写入仍由原调度/交易负责人完成；只读模块不打开数据库、不联网、不运行迁移。
- `record_kind`、版本、内容指纹、状态词表和上海业务日计算保持。
- 坏 JSON、未知状态、非法时间、未来事实、被重试覆盖的历史事实继续拒绝或标为不可用。
- 旧入口的数据类型和新入口是同一个对象，不复制声明或包装成第二套事实类型。

## 验证记录

- 初步相关回归 75 项通过；新增独立进程验证研究采集不导入交易/自适应主模块，旧入口的 16 个公开对象与新入口身份相同。
- 搬出 608 行实现；`paper_trading.py` 从 15,780 行降至 15,188 行。搬出实现与剩余交易代码均通过与本轮前工作树的语法结构等值检查。
- 研究汇聚反例矩阵 23/23 被捕获；迁移后的两项真实故障注入 2/2 被捕获，34 项前置目标全部通过，源码哈希还原通过。同步工具里已经失效的 INC32 测试名，未绕过其基线检查。
- 4 项独立性/旧入口身份/标准库依赖/只读查询测试通过；ruff、compileall、差异格式检查通过。
- 完整后端测试：`python -X utf8 -W ignore::ResourceWarning -m unittest discover -s backend -p 'test_*.py' -v`，6,034 项，6,025 通过、9 跳过，0 失败、0 错误，耗时 946.768 秒。跳过项与前轮一致（8 项依赖 Linux 环境，1 项重复黄金回放由子类覆盖）；没有新增跳过条件。
- 无需新增配置或依赖，原公开调用方式继续可用。本批只完成任务运行事实职责提取，未搬迁选股流程或成交编排。
- 后续批次按 `docs/SOURCE_SLIMMING.md` 的职责和验收条件推进。

## 第二批：选股应用流程

将 `main.py` 的选股流程、因子缓存读取、上下文刷新、资金流刷新与结果缓存迁入 `selection_service.py`。网页入口保持参数和状态码；`paper_selection` 与两个 runner 直接使用共享实现或交易日历。保留现有取数、因子与交易模块，不增加通用服务框架。

- [x] 核对主模块、缓存、后台线程、选股任务及测试的真实依赖。
- [x] 提取实现并迁移调用方；测试跟随真正持有状态的模块。
- [x] 验证搬迁等值、接口衔接、独立进程及并发更新边界；执行反例故障注入。
- [x] 完整后端检查通过后记录交付结果。

本批前工作树快照在 `.codex/selection-baseline/`；保留第一批及此前全部未提交改动。新模块返回普通数据，未知策略的 HTTP 400 转换留在网页接口；定时任务继续同步获取财务/情绪上下文，网页继续后台刷新。旧结果持久化、完整交易日、实时覆盖、策略版本来源与排序必须保持。

第二批验证：45 个搬出声明、主模块剩余 70 个声明与本批前源码语法结构等值；3 个调用方仅改变依赖方向。相关回归 226 项通过（1 个既有重复回放跳过），新增独立进程、网页接口/持久化、并发刷新代次和过期因子拒绝测试。对抗性检查 8/8 错误被断言捕获（缓存代次丢失、过期键丢失、资金流文件键丢失、网页同步等待、未完成结果落盘、过期因子放行、慢源重复线程、未知策略错误状态码）；8 项前置基线全部通过，源码哈希未变。

响应对照：使用本批前工作树中的真实选股函数和相同的非空行情/因子输入，新旧完整字典相等；候选顺序、执行/观察数量、报价来源和 14 个数据质量字段一致，未完成日线未进入执行判断。低覆盖拒绝字典也一致。日志位于 `.codex/selection-parity.log`。

最终完整后端检查：`python -X utf8 -W ignore::ResourceWarning -m unittest discover -s backend -p 'test_*.py' -v`，6,039 项，6,030 通过、9 跳过，0 失败、0 错误，耗时 1,051.966 秒，进程退出 0；日志 `.codex/selection-split-full.log`。9 项跳过的名称与原因逐项和第一批一致，没有新增跳过。新增 5 项测试全部通过，7 个受影响的既有测试文件没有删减测试方法。最终源码等值、ruff、compileall 与差异格式检查均通过。

本批 `main.py` 2,145→1,402 行，新增的 `selection_service.py` 779 行。无需新增依赖或配置，后续按 `docs/SOURCE_SLIMMING.md` 拆候选装配与订单编排。

## 第三批：候选截面装配与纯筛选

将 `_candidate_rows` 中「取数之后 → 截面装配 → 证据附加」这一段迁入 `paper_candidate_assembly.py`，并把运行期可执行性谓词分离到 `paper_candidate_projection.py`。因子文件读取、新鲜度判定与覆盖率阈值继续留在交易主编排：这些模块级阈值会被黄金回放和架构护栏直接改写，另存一份就会形成两个真相源。

- [x] 核对 `_candidate_rows` 的真实自由变量、辅助函数调用关系与被补丁位置。
- [x] 搬出板块/同业证据与输入装配，调用方改为接收普通字典。
- [x] 分离纯筛选谓词，测试跟随真正持有实现的模块。
- [x] 验证结构等值、负载契约、独立导入、行为回归与对抗性反例。
- [x] 完整后端检查通过后记录交付结果。

### 边界

- 保留在交易主编排：`_selection_inputs`、`_selection_factor_freshness` 及其缓存/清单签名链、`CANDIDATE_FACTOR_MIN_ROWS`、`CANDIDATE_FACTOR_MIN_COVERAGE`、`SELECTION_FACTORS_PATH`、`SELECTION_META_PATH`。`test_production_path_golden_replay`、`test_candidate_column_reads` 直接改写这些属性，它们必须只有一个持有者。
- 装配模块以参数接收覆盖率阈值（`min_factor_rows`、`min_factor_coverage`），因此仍是单一配置源；模块内不导入 `paper_trading`。
- 交易日滞后计算 `_trading_weekday_lag` 迁入 `paper_trading_rules.trading_weekday_lag`（该文件本身就是可重复规则的家，且已被两个模块依赖），避免候选模块和交易模块各存一份日历算法。
- 策略分发、候选后处理、点火影子写库、观察池冻结、板块车道注入、最终返回值组装全部留在原处；三段结构里只有「输入装配」被搬出。

### 阶段结果

- 搬出 6 个板块/同业证据函数（420 行）与装配阶段（原 280 行代码 + 负载字典），新建 `paper_candidate_assembly.py` 777 行；`paper_trading.py` 15,188→14,460 行（减少 728 行），`_candidate_rows` 742→493 行。
- `paper_candidate_projection.py` 新增 `risk_flag_codes`、`filter_executable_candidates` 两个纯谓词（+27 行），三个运行期条件（日内 T 资产限制、风险标的排除、非正价格）原顺序保留；容量与整手判断继续留在执行层。
- 结构等值：6 个搬出函数与 777 行模块内的板块函数逐一与基线 `ast.dump` 相同；装配函数 50/50 条语句与基线切片相同；`_candidate_rows` 头部 6 条语句的语法结构与基线逐条相同，尾部到运行期筛选之前逐条相同，末条 payload 只差 `_flow_supplemented_codes`→`flow_supplemented_codes` 这一次键名规范化。
- 负载契约：装配返回 20 个键，调用方解包 19 个名字全部有对应键，没有多余键；`first_board_codes` 仍由调用方的 `_selection_inputs()` 提供。
- 名称解析：装配模块每个自由名字都可解析，且不导入 `paper_trading`（循环依赖为零）。

### 验证记录

- 对抗性检查 9/9 被断言捕获：负载缺键、调用方不再传策略模式、不再传风险排除集、删掉因子滞后过滤、改动板块热度语义、装配模块回引交易主模块、删掉价格谓词、删掉 ST/退市判定、删掉日内 T 资产限制。全部在临时副本或内存模块中注入，未改动工作树。
- 测试跟随实现位置：`test_candidate_column_reads` 的板块证据补丁改指装配模块；`test_selection_point_in_time` 的实时截面时点护栏改指 `assemble_candidate_inputs`（三处 live 调用点数量与断言强度不变）；`test_architecture_optimization` 的导入边界允许投影模块使用 `paper_numeric` 与 `paper_trading_rules`，并新增按**函数名**的子集校验（只放行 `asset_type`/`security_scope`/`is_st_or_delisting` 三个纯谓语）。
- 新增 3 项谓词测试（风险标的识别、运行期过滤顺序与零价剔除、日内 T 资产限制），既有测试方法没有删减或跳过。
- 完整后端检查：`python -X utf8 -W ignore::ResourceWarning -m unittest discover -s backend -p 'test_*.py' -v`，6,042 项，全部通过、9 跳过、0 失败、0 错误，耗时 1,048.062 秒，日志 `.codex/candidate-split-full.log`。9 项跳过的名称与原因与前两批一致，没有新增跳过。
- `ruff check backend`、`compileall -q backend`、差异格式检查通过；搬出的 `_flow_supplemented_codes` 在源模块保留原名，只在返回负载里改名。

本批未拆订单推进（`_buy_order` 1,142 行），后续按上表第 4 项继续。

## 第四批：订单推进的金额与成本判定

`_buy_order` 的三段（读取事实 / 计算提案 / 事务内重验）中，**读取事实这一段目前不可安全分离**：它对 7 个事实读取函数的传递闭包是 32 个函数 848 行，包含 `_ensure_cycle`（225 行，会创建周期）和 `_reconcile_shared_cash`（57 行，会调整共享现金）。把它们搬进只读模块就等于把写库能力搬到只读边界之外，因此本批只提取其中确实纯的一段。

- [x] 按传递闭包证明读事实段的耦合范围，确认不能在本批搬走。
- [x] 提取「金额与成本判定」到 `paper_entry_decision.assess_order_economics`（该模块本就是纯决策 owner）。
- [x] 用基线原文做合成函数逐组比对，证明行为等价。
- [x] 对抗性反例、相关回归与完整后端检查。

### 边界

- 搬出内容：最终数量 → 金额/费用/一手金额的换算，以及一手不足、尘埃单、现金不足、最低佣金占比、有效委托金额、往返成本覆盖六类金额侧判定（原 101 行）。
- 留在 `_buy_order`：读取待成交预占（唯一需要读库的事实）、策略经济性策略与风险画像的解析（作为参数注入）、全部硬门禁（双源行情、Q 级、追高、时机、席位、共享池、风控状态、分配快照复核）、以及订单落库与成交提交。
- 硬/软分组原样保留：现金为零是硬否决，余额不足可重试；`risk` 单独绑定时结构性不足转终态拒绝，预算侧约束（cash/weight/exposure/industry）进等待池。
- 读取待成交预占的位置从金额计算之后上移到判定调用之前，仍在同一个无写入的读取区间内，不改变结果。

### 验证记录

- **穷举等价证明**：把基线 `_buy_order` 第 8811-8911 行（101 行）原文作为合成函数执行，与新函数在 **69,120** 组输入组合上逐字段比对（金额、费用、7 个 sizing 字段、新增原因、软约束分组、顺序），**全部一致**。日志与脚本 `.codex/check_economics_equivalence.py`。
- 结构检查扩展：`_buy_order` 与基线的差异窗口被限定为「14 条语句 → 7 条语句」，窗口首句必须是 `amount = qty * fill_price`、末句必须是 `if econ and amount > 0:`，替换后的 6 条固定语句与注入的 11 个关键字逐个校验；其余 187 条前缀语句与 34 条后缀语句与基线 `ast.dump` 相同。
- 对抗性检查累计 **11/11** 被捕获：新增的「经济性调用丢失卖出估价注入」「调用方不再合并软约束原因」与既有 9 项（负载缺键、策略模式/风险排除集不再传递、删掉滞后过滤、改动板块热度、装配回引主编排、删掉价格谓词、删掉 ST 判定、删掉日内 T 限制）。
- 另有 **5/5** 订单经济性反例被断言捕获（现金否决降级为软约束、结构性止损不足改为可等待、尘埃单失去软分类、删掉往返成本覆盖要求、一手金额误用全量数量）。
- 新增 5 项常驻测试（零现金硬否决/余额不足软约束、risk 结构性 vs 预算侧绑定、尘埃单地板、经济性策略的金额与往返成本门槛、一手金额与 sizing 不被修改）。
- 相关回归 166 项通过（`test_strategy_buy_commit_convergence`、`test_execution_planner`、`test_paper_capital_reservations`、`test_entry_capital_asof`、`test_paper_shared_cash`）。
- 完整后端检查：6,047 项，全部通过、9 跳过、0 失败、0 错误，耗时 1,111.391 秒，日志 `.codex/economics-split-full.log`。
- `paper_trading.py` 14,460→14,376 行，`_buy_order` 1,142→1,058 行，`paper_entry_decision.py` 自 129 行增至 244 行。`ruff check backend`、`compileall -q backend` 通过。

### 下一步（第 4 项的剩余部分）

要真正把 `_buy_order` 拆成三段，必须先有独立批次处理前置依赖：把周期/现金生命周期（`_ensure_cycle`、`_reconcile_shared_cash`、`_economic_pool_nav_history`、`_account_reference_capital` 等）从交易主编排剥离到各自的写入 owner，让事实读取能够只依赖只读连接。在此之前强行拆分会把写库能力带进只读边界，或复制第二份资金口径。

## 第五批：入场评分模型

`_buy_order` 的三段式拆分卡在周期/现金 owner 上，因此本批先拆同一路径上确实独立、且全是纯计算的那一块：入场评分模型。

- [x] 用传递闭包确认评分模型零数据库、零时钟、零网络依赖。
- [x] 原样搬出四个函数，主编排保留同身份兼容导出。
- [x] 账户 spec 解析保持单一 owner（主编排绑定，评分模块不自行解析）。
- [x] 结构等值、绑定同一性、对抗性反例与完整后端检查。

### 范围

- 搬出 `_clip01`、`_microstructure_soft_factor`、`_dsl_entry_assessment`、`_strategy_entry_assessment`（4 个函数，558 行）到 `paper_entry_assessment.py`（616 行）。输入是已冻结的候选、实时报价、已收盘日线、盘口微结构与市场灯色，输出是入场评估字典。
- `paper_trading` 保留 `_clip01`、`_strategy_entry_assessment` 的同身份兼容导出，`_signal_approval` 与 `_buy_order` 的调用签名不变。
- `_spec_for` 需要注册表运行时上下文，属于交易主编排：评分模块只保留同名解析口，主编排在 `_spec_for` 定义之后调用 `PEA.bind_spec_resolver(_spec_for)` 绑定。独立导入时退回声明层与保守回退，绝不猜测注册表。
- 未搬：`_new_listing_entry_assessment`（新股专用评估）、`_signal_approval`（证据收集与审批编排）、以及全部 `_buy_order` 硬门禁与落库。

### 验证记录

- 结构等值：4 个函数逐一与基线 `ast.dump` 相同，且 `paper_trading` 中不再有同名定义。
- 绑定同一性：13 个共享名字（`_num`、`_limit_pct`、三个板块阈值上下文、策略标识、`RISK_PROFILES`、`MAIN_FORCE_STRATEGY_VERSION`、`IGNITION_BUY_ENABLED`、`IGN`、`pd`、`ACS`）在新旧模块里**是同一个对象**；三个账户（含未知账户）的 `_spec_for` 结果与主编排逐一致。
- 纯计算边界：评分模块不导入 `sqlite3`/`paper_trading`/`data_fetcher`，不调用 `open`/`read_csv`/`connect`/`execute`/`now`/`today`/`time`。
- 对抗性检查 **6/6** 被捕获：spec 解析口未绑定、评分权重被改动、模块引入 sqlite3、旧 helper 在两个模块各留一份、审批路径不再调用共享评估、搬出的函数体与基线不再相同。全部在临时副本注入，未改工作树。
- 新增 1 项常驻边界测试（评分模型纯度 + spec 单一解析口 + 兼容导出同一性）。
- 相关回归 102 项通过（`test_architecture_optimization`、`test_strategy_entry_guardrails`、`test_candidate_column_reads`、`test_selection_point_in_time`）。
- 完整后端检查：6,048 项，全部通过、9 跳过、0 失败、0 错误，耗时 1,060.167 秒，日志 `.codex/assessment-split-full.log`。
- `paper_trading.py` 14,376→13,820 行。`ruff check backend`、`compileall -q backend` 通过。

### 周期/现金 owner 为何仍未拆

调查结论记录在案，供后续批次直接接手：

1. `_active_cycle` 会在缺少周期时调用 `_ensure_cycle` **懒创建周期**，而 `_buy_order` 读事实的第一步就是 `_active_cycle`。要让读事实只读，必须先决定"谁有权引导创建周期"，这是行为变更，不是纯搬迁。
2. `test_paper_account_specs` 断言 `ACS.ACCOUNT_SPECS is PT.ACCOUNT_SPECS`、`ACS.RISK_PROFILES is PT.RISK_PROFILES`；`test_paper_user_cycle_attachment` 与 `test_paper_cycle_ownership`/`test_paper_cycle_capital` 直接 `patch.object(PT, "ACTIVE_ACCOUNT_IDS", ...)` 来验证降级行为。常量一旦另存一份，这些护栏改的就不是生产实际读的那份。
3. 该域传递闭包为 22 个函数 482 行，其中 `_ensure_cycle` 225 行、`_reconcile_shared_cash` 57 行都会写周期与共享现金。

因此本批不做，避免在资金与周期语义上留下未验证的改动。


