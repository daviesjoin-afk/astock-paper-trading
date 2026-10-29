# 源码公共层与瘦身审计

日期：2026-10-28。基准：分支 `codex/intraday-scan-risk-hardening-20261028` 的 `0d8bbc8`，另含工作区尚未提交的 C2 策略工坊改动。本文是源码审计与分批整改清单；静态搜索不能证明所有动态调用、线上频率或计算耗时。本轮保留已有未提交改动，没有修改交易或资金规则。

**本轮（整改）状态：第 1、2、3、4、6 项已完成并有回归覆盖；第 5 项完成两个业务簇的下沉；第 7 项完成设置中心。仍未完成的部分见文末「尚未完成的整改」。**

## 原则

公共层应当统一事实来源和明确的规则，并在同一轮、输入完全一致时复用已经取得的结果。不能把不同周期、时间截点、行情新鲜度、复权口径、账户和权限状态的结果混为一谈。重复读取与重复计算需要分别计量；简单把代码移动到公共函数并不等于减少计算。空结果和异常是否缓存、调用方是否会修改返回值，都要由契约与测试决定。

## 本地静态基线

用仓库 `backend/tools/coupling_metrics.py` 扫描 backend 顶层非测试生产模块，共 313 个；该范围不等于全仓所有嵌套包，也不覆盖动态导入。前端 `src` 有 20 个 JS 文件。导入环 0、延迟导入 618、涉及 execute 的模块 141、跨域边 614、跨模块私有访问 201。同名顶层函数名 312 种，数值 helper 定义 54 个；这些只是排查线索，不代表语义重复或可删除数量。

最大文件 `paper_trading.py` 15,720 行；`adaptive_engine.py` 4,639 行；`learning_evaluation.py` 2,825 行。最大函数 `paper_trading._buy_order` 1,195 行。行数包括注释和空行，规模不直接证明运行瓶颈。机器可读结果保存在本地 `work/source-audit-metrics-20261028.json`；扫描脚本为 `work/source-audit-metrics-20261028.py`，均是本地审计记录。

`python -m pytest backend/test_architecture_boundary_audit.py -q` 通过 3 项。该门禁只说明当前指标未超既有预算，不能证明本报告发现的基础层依赖方向缺口不存在。

## 已确认发现

### 1. 手动委托同次规划重复读取相同日线【优先】【已整改】

`backend/manual_orders.py:376` 的买入决策与 `:550` 的决策快照都读取 `(code, day, inclusive=False)`；卖出分支 `:431` 与 `:550` 也一样，而 `:429` 的持仓天数通过 `paper_risk_evidence.hold_days` 在 `backend/paper_risk_evidence.py:84` 再次读取相同参数（有持仓时）。`backend/data_fetcher.py:1730` 的公共日线读取会调用 `load_cached_kline`，后者读取 CSV、解析和排序。这里确认的是重复调用和读取路径，未测量实际耗时。

**整改**：新增 `backend/paper_kline_snapshot.py`（`RoundKlineCache`），按 (证券, 截止日, 是否含截止日) 在**一次规划内**复用一次成功读取的日线，每个消费者拿到独立副本；空结果不缓存，同一轮内可重试。`manual_orders` 的买入决策、卖出模型、持仓天数与决策快照四处读取全部走这一个复用器。

**验收**：`backend/test_manual_order_kline_replay.py` 用**可切换的对照实现**（"永不缓存"版 = 改造前语义）做黄金回放，逐项比较模型、数量、理由、是否放行与决策快照，并断言读取次数下降；另覆盖失败矩阵（空结果重试、消费者修改不串、下一次规划重新取证、缺失日线两种路径结论相同）。`test_manual_order_round_kline.py` 2 项通过，`test_manual_order_kline_replay.py` 9 项通过。

**仍待完成**：性能计量（真实 CSV 读取耗时）未做；改造前后的**生产数据**回放（用真实历史账本跑同一批候选并比对决策）未做 —— 现有回放是夹具级。

### 2. 公共日线截断异常时返回未截断数据【高风险】【已修复】

`backend/data_fetcher.py:1730` 的 `completed_kline` 对索引日期比较失败捕获异常后直接返回原始 frame。正常 CSV 加载会整理索引，但异常情况下原始 frame 未经截止日过滤，理论上可能包含截止日之后的数据。

**复现**：混合时区字符串索引（`2026-01-05T00:00:00+08:00` 与 `2026-01-06T00:00:00+00:00`）会让 `index.date` 直接抛 `AttributeError`；旧实现捕获后返回**整个未截断 frame**，含截止日之后的行。

**修复**：截断语义改为 fail-closed。新增 `_kline_bar_dates` 逐行解析自然日（保留索引自身的本地日期，不做 UTC 归一化）；日期无法解析的行被**丢弃**（"无法确认是否越界"等同于越界风险）；索引整体不可用时返回空帧而不是原始帧。`backend/test_completed_kline_truncation.py` 7 项通过，覆盖越界截断、混合时区、不可解析日期、全垃圾索引、空源与来源元数据保留。

**仍未做**：没有用真实线上 CSV 复现过（需要一份混时区的历史缓存）；没有对历史决策做回放，判断过往是否真的用过越界数据。

### 3. 选股轮次缓存缺少输入身份校验【边界待验证】【已整改】

`backend/selection_application.py:97` 的 `SelectionRound` 在同一轮直接存 `snap_f` 与 `fund_f`，`:671` 命中时不比较股票集合、财务输入或截止时间。

**整改**：轮次缓存的每一项现在都带**输入身份**，身份由缓存内容自身推导（不是由调用方登记）：
- 实时快照：报价时间所属的交易日 + 覆盖的股票集合。跨交易日边界的旧快照作废并重新取证。
- 截面：覆盖的股票集合必须仍然覆盖当前应当参与的股票。
- 基本面：覆盖的股票集合 + 产生它时的财务输入标识（报告期）。财务上下文被后台刷新后身份改变 → 不复用。
- 身份**推导不出来**的缓存项一律视为不可复用（fail-closed）：宁可多算一次，也不把来源不明的旧结果当成"同一份输入"。

**验收**：`backend/test_selection_round_identity.py` 7 项通过（身份推导、覆盖判定、跨交易日必须重新取证、同日必须复用、基本面无身份不复用、forget 语义）；`test_selection_round_inputs.py` 1 项通过（原夹具已改为成对登记基本面与其财务标识）。

**仍未做**：没有覆盖"同一轮内财务上下文被后台刷新"这一真实并发场景的端到端回放。

### 4. 度量器不能证明基础层依赖方向【已确认口径缺口】【已整改】

`backend/tools/coupling_metrics.py:101` 使用名单确定 foundation，`:212` 将涉及 foundation 的双向边都排除。单向 foundation → 业务域的公开导入并不必然产生环或私有访问。

**整改**：新增 `coupling_metrics.foundation_violations()` —— 基础层 → **业务域**的单向依赖是方向错误，基础层**之间**的依赖不算（它们同属这一层）。白名单 `FOUNDATION_ALLOWED_DEPENDENCIES` 只登记确实越过层边界的例外，每一条都写明理由（执行验证谓词、持仓聚合 owner、审计戳 owner、诊断计时器、交易日历与涨跌停阈值）。`test_architecture_boundary_audit.py` 新增两项守卫：反向依赖必须为空；白名单不得有未使用条目、不得登记基础层模块。已用临时副本验证守卫确实能抓到 `paper_numeric → paper_trading` 这类反向依赖。

### 5. 主引擎与依赖包仍需按业务簇瘦身【已确认结构热点】【部分完成】

**已下沉两个业务簇**（新增两个 owner，`paper_trading.py` 15,720 → **15,028 行**）：
- `backend/paper_candidate_lanes.py` —— 板块热度图、板块异动直通车道、概念反查与概念扩张车道、同花顺热门车道、科创板板块脉冲，共 8 个函数。
- `backend/paper_entry_price_rules.py` —— 新开仓价格门禁与开盘追高时段余量。门禁保持纯函数：账户 spec 由引擎解析后作为**事实**传入，用户 DSL 策略不会被内置声明误导。

`backend/test_public_layer_slimming.py` 12 项通过：新 owner 不反向依赖交易引擎、不写账本、价格门禁保持纯判定、引擎 facade 与 owner 判定逐项一致、证券权限仍是唯一资格入口。

**仍未完成**：
- `_candidate_rows`（`paper_trading.py:5153`，774 行，**61** 个全局引用）**没有**下沉。它依赖 `_spec_for` / `_selection_inputs` / `_main_force_watch_pool` 等大量引擎内部；直接切开只是把耦合搬到新文件。正确顺序是先把 `_selection_inputs`、`_main_force_watch_pool`、`_ignition_shadow_store`、`_attach_candidate_financial_disclosure` 各自归位，再动 `_candidate_rows`。本轮**不**动它，并把全局引用额度显式记为实测值 61（额度 0），而不是掩盖这个事实。
- `_buy_order`（1,195 行）未动。
- `adaptive_engine.py`（4,639 行）与 `learning_evaluation.py`（2,825 行）未动。

### 6. 同名数值 helper 不能机械归并【已确认不同合同】【已整改】

`backend/paper_numeric.py` 已明确区分严格数值合同与允许字符串转换的另一合同。

**整改**：在 `paper_numeric` 补齐五个**语义明确、互不相同**的 owner，并建立可执行的输入矩阵（空值、布尔、数字字符串、NaN、无穷大、异常对象）：

| 函数 | 合同 | 合并进谁 |
|---|---|---|
| `num(value, default)` | 严格：只接受真正的 int/float；字符串拒绝；布尔接受（历史行为） | 既有 |
| `coerced_number(value)` | 宽松解析 + 严格有效：接受数字字符串；布尔/NaN/无穷 → `None` | 5 个报告/影子车道模块 |
| `bounded_num(value, default)` | 幅度上限 1e15；其余按 default | 3 个 AI/进化模块 |
| `finite_num(value, default)` | 拒绝 NaN/无穷 | 4 个 adaptive/归因模块 |
| `nullable_num(value, default)` | `None` → default；**保留**无穷 | 2 个持仓模块 |
| `strict_finite(value)` | 只接受真实测量值；`nan`/`none`/`null`/`-`/`inf` 字面量 → `None` | 2 个学习/评估模块 |

合并了 **16 份**逐字相同的实现（5 + 3 + 4 + 2 + 2），覆盖空值、布尔、数字字符串、NaN、无穷大与异常对象六类输入。`backend/test_numeric_contract_matrix.py` 13 项通过，既锁住每个合同的行为，也锁住**不同合同必须保持不同**（否则合并就改变了风控口径）。

**刻意没合并**：`paper_position_read_model` 与 `paper_position_review` 虽是同一实现，但它们是**纯域**模块（有"零项目导入"机械守卫），所以保留自己的实现；`test_numeric_contract_matrix.py` 显式断言两者行为与共享 owner 一致。

### 7. 前端桥接仍有后续空间【已确认】【部分完成】

**设置中心已迁移到页面事件委托**：新增 `installSettingsActions`（`frontend/src/features/settings.js`），静态按钮、重绘后的动态按钮与二级导航都通过 `data-settings-action` 声明，由一个委托统一分发；`frontend/src/features/ai-provider-settings.js` 与 `ai-capability.js` 的内联处理器一并迁移。`frontend/src/bridge.js` 的 `window` 赋值 **86 → 68**，设置相关的 18 个全局全部消失。

**验收**：`frontend/tests/settings-action-delegation.test.mjs` 3 项通过（无内联处理器、无全局桥回流、已渲染动作与分发表逐项一致、二级导航分组完整）；`frontend/e2e/specs/settings-actions.spec.js` 3 项通过（真实浏览器里的分组切换、解锁/清除、window 上不再有这些函数）；`frontend/e2e/specs/operator-unlock.spec.js` 6 项、`ai-providers.spec.js` 3 项、`bridge-contract.spec.js` 6 项全部通过。`backend/test_frontend_module_contract.py` 新增一项方向守卫。

**仍未完成**：`bridge.js` 仍有 **68** 个 `window` 赋值（进化中心与策略工坊之外的页面）；本轮**没有**逐页迁移进化中心。

## 优先顺序与验收

1. ~~先复现公共日线异常及确认边界；建立可重复的输入输出用例。~~ **已完成**（第 2 项）。
2. ~~做手动委托单次规划日线复用，对比买卖黄金回放、缺数据和不同截点，并测实际读取次数。~~ **部分完成**（第 1 项：夹具级黄金回放与失败矩阵已完成；生产数据回放与性能计量未做）。
3. ~~验证选股轮次缓存身份；补齐输入变化、跨截止日、结果修改和失败恢复测试。~~ **已完成**（第 3 项）。
4. ~~为基础层依赖方向加独立守卫，审查每个候选基础模块是否真的没有业务依赖。~~ **已完成**（第 4 项）。
5. 按业务簇推进主引擎归位，逐簇做黄金回放；前端继续逐页迁移。 **进行中**（第 5 项完成两个簇；第 7 项完成设置中心）。
6. ~~逐类盘点同名 helper、旧路径和研究功能，保留真实调用与历史数据所需路径；删除必须有调用与数据证据。~~ **helper 部分已完成**（第 6 项）；旧路径与研究功能的删除盘点未做。

## 尚未完成的审计

`work/classify-repeated-calls.py` 把粗筛的 120 条候选按 AST 上下文重新分类：**真正处于同一直线路径、无分支/循环/写边界**的只有 **8 条**，其余 47 条是互斥分支或循环，另有 65 条是廉价的字典/属性访问（不省 I/O 也不省计算，已从候选里排除）。8 条已**逐条核实**并记录结论在 `work/audit-repeated-calls-classified-20261028.json` 与脚本的 `VERIFIED_EXCLUSIONS` 里：

| 候选 | 结论 |
|---|---|
| `manual_orders._manual_order_plan` 的 `round_kline` | **已整改**（第 1 项） |
| `selection_application._select_uncached` 的 `_quote_age_ok` | **已整改**：资金流行的新鲜度判定只做一次，两处共用同一份结果 |
| `paper_trading.risk_dashboard` 的 `snapshot_age_seconds` | **不合并**：两次面对的是**不同快照**（中间可能换成占位快照或刷新结果），早期值只用于陈旧度门控 |
| `strategy_champion.run_shadow_context` 的 `_json_loads` | **不合并**：两次必须各拿一个独立副本，runner 可以修改自己那份 |
| `data_fetcher._fetch_tencent_market_snapshot_locked` 的 `usable_quote` | **不合并**：一次判定腾讯行，一次判定新浪备用行，对象集合不同 |
| `tools/modularity_reuse_bench.main` 的 3 条 | **不合并**：基准工具的故意重复读取 |

跨事务重新读取账户（`adaptive_risk` / `adaptive_selection`）、取锁后重读（`tracker.py`）、不可序列化回退与缓存未命中的不同分支（`paper_candidate_inputs.py`）都保留原有复核或回退。**未核实的 duplicate 候选：0。**

本报告仍未逐函数验证全仓所有数据源、所有执行路径、数据库查询、前端动作与研究功能，也未测生产耗时、内存、模型费用和线上使用率。

