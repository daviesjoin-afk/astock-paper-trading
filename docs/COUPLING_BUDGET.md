# 耦合预算与模块化进度

2026-10-28（公共层瘦身审计切片）：主引擎按业务簇继续下沉，新增两个 owner ——
`paper_candidate_lanes`（板块热度图、板块异动/概念扩张/同花顺热门车道、
科创板板块脉冲）与 `paper_entry_price_rules`（新开仓价格门禁与开盘追高
时段余量）。`paper_trading.py` 15,720 → **15,028 行**，最大文件额度收紧。
新模块带来 **5 条**新的跨域 import 边（候选车道 → `universe`/`alt_data`/
`strategies`，门禁 → `strategy_policies`），额度 4 → 9；这是"归属明确化"的
代价，不是新增业务耦合。`延迟 import` 618 → 619（`paper_candidate_lanes`
里 `alt_data` 的可选导入兜底），额度 35 → 36。

2026-10-28（公共层瘦身审计切片）：`_candidate_rows` 仍是 `paper_trading`
里全局引用最多的函数（60 → **61**，额度 1 → 2）。它依赖大量引擎内部
（`_spec_for` / `_selection_inputs` / `_main_force_watch_pool` 等），
本轮**不**动它：把它切开需要先把这些能力各自归位，否则只是把耦合
搬到新文件里。等价地，`_candidate_rows` 的全局引用数不会因为本轮
下沉而下降，额度因此显式登记而不是静默放宽。

2026-10-28（A8 收尾）：最后两组环关闭后 `import 环` 由 2 降为 **0**，门禁改为
「一个环都不允许」。本次同时收紧 `延迟 import`（39 → 35）、`跨域 import 边`
（42 → 40）与 `跨模块私有属性访问` 基线（202 → 201）；`顶层 import 副作用`
23 → 24，来自组装期的一次 `bind_default_runner` 调用，仍在基线 25 以内。

2026-10-28：入场时机状态的跨进程文件锁需要按平台加载 Windows 标准库
`msvcrt`，延迟导入额度由 38 增至 39；这是平台兼容导入，不增加项目模块依赖。

基线：PR272 HEAD `961c09f`。度量入口：`backend/tools/coupling_metrics.py`，由 `backend/test_architecture_boundary_audit.py` 执行不回退门禁。分析器只读 AST，不 import 生产模块。

## 当前预算

| 指标 | PR272 基线 | 本次收口后 | 允许增量 | 长期目标 |
|---|---:|---:|---:|---:|
| import 环 | 5 | **0** | 不允许任何新环 | 0 |
| 延迟 import | 583 | 619 | 36 | 逐步降低；仅移除无必要的延迟依赖 |
| 裸 SQL 模块 | 135 | 141 | 6 | ≤60 |
| paper 表最大模块 fan-out | 49 | 50 | 1 | ≤8（逐表 owner/read model） |
| 跨模块私有属性访问 | 201 | 201 | 0 | 0 |
| 跨域 import 边 | 624 | 619 | 9 | 分阶段降低 |
| 最大文件 | 16367 行 | 15028 行 | 0 | ≤2500 |
| 最大函数 | 1191 行 | 1195 行 | 4 | <200 |
| 单函数最大全局引用 | 56 | 61 | 2 | <80 |
| 重复顶层 helper 名 | 307 | 313 | 6 | <30 |
| `_num`/`_number`/`_finite` 重复定义 | 54 | 54 | 0 | 按语义逐模块收敛，不按名称批量替换 |
| 无归属前缀模块占比 | 41.39% | 39.56% | 0 | <30% |
| 顶层 import 副作用 | 25 | 24 | 0 | <5 |

允许增量来自本次审核修复必须新增的生产模块与调用点（`selection_application`、`paper_runtime_facts`、`research_pipeline`、`research_candidate`、`research_orchestration`、`research_runtime_config`、`paper_account_metrics`）。允许增量是显式预算，不是重设基线；未来任何恶化仍会失败。`paper_trading.py` 已降至 15,622 行，最大函数仍是 `_buy_order`，未做高风险重写。

## 本次切片

- 2026-10-28（A8 收尾：最后两组环，环 2 → 0）
  - **`market_data_service` ↔ `universe`**。新的下层事实 owner `data_universe_store`
    承接 `universe.json` 的读取边界（路径、`(路径, mtime, size)` 轮内复用、正式清单门槛），
    `universe.load_universe()` 与行情备用报价都向它取同一份代码集合，行情侧不再
    import 基础库构建流程。**关键点**：正式清单门槛 `FORMAL_UNIVERSE_MIN_ROWS` 随读取一起
    搬到 store 并在调用时查找——门槛必须和"按门槛判定"的那份读取同属一个 owner，否则
    只要有人改一侧，就会出现「读取方认为清单合格、构建方认为不合格（或反之）」的隐性
    不一致。`universe.build_universe()` 因此直接读 `USTORE.FORMAL_UNIVERSE_MIN_ROWS`。
    测试侧只改缝的位置，不改断言：腾讯回退的 3 处 patch 目标从 `universe.load_universe`
    改为 `data_universe_store.load_formal_universe`，回放夹具的规模放宽目标从
    `universe.FORMAL_UNIVERSE_MIN_ROWS` 改为 `data_universe_store.FORMAL_UNIVERSE_MIN_ROWS`。
    新增 `test_data_universe_store`（8 项）钉住下沉后的判据本身：正式清单门槛、
    `scope` / `requested_limit` 的老文件防误用、**缓存身份含路径**（两个路径不得互相串用）、
    重建后按文件签名失效、读失败不被记忆成成功，以及 `universe.UNIVERSE_PATH` 这条部署
    seam 仍然有效。
  - **`strategies` ↔ `strategy_plugins`**。插件表不再 `import strategies`；需要原生算法的
    插件通过 `bind_default_runner()` 由算法 owner 在**组装期**绑定，`strategies.py` 模块级
    那一行就是唯一绑定点。默认 runner 是 `_plugin_default_runner`，内部仍按模块全局
    `_run_strategy_legacy` 在**调用时**查找，所以替换该全局的测试/组装方语义不变。
    插件对象自身**没有**被塞进 `candidate_runner`——一旦塞进去，`is_original_builtin_plugin`
    会变成 False，`scoring_factor_inputs` 就会退回"全量输入"，属于行为变更。未绑定就调用
    默认路径时报明确错误，而不是在 import 处埋一个隐性顺序依赖。
  - 顺带收紧：`strategies` 里 3 处函数内 `import strategy_plugins` 上移到模块级（少 3 个
    延迟 import），`strategy_plugins` 里 `S._run_strategy_legacy` 这处跨模块私有属性访问消失。
- 2026-10-28（A5 任务生命周期）：`paper_jobs` / `paper_job_runs` 的 **9 处**状态写入（认领探测、运行中过期判失败、首次 INSERT、重试认领 CAS、心跳续期 CAS、完成 CAS、失败 CAS、启动回收、历史清理）从 `paper_trading` 收进新 owner `paper_job_lifecycle`，语句只有一份，`paper_trading` 里对这两张表的 INSERT/UPDATE/DELETE **归零**（`test_paper_job_lifecycle` 用 AST 扫描 + 常量渲染做机械守卫，含空结果非空性）。行为不变：owner 不 commit、不做"该不该重试"的业务判断，CAS 谓词（`owner_key` + `fencing_token` + `status='running'`）逐字保留。裸 SQL 模块 140 → 141（SQL 换了归属，模块数多一个，额度 +1 已登记）；主文件 `paper_trading.py` 少 84 行。
- 2026-10-27（§13.4 复用资源测量）：`backend/tools/modularity_reuse_bench.py` 把「同轮输入复用」的净效果做成可重跑证据 ——
  三个消费者、5000 只证券、合成输入、不联网不碰生产库。分开取数时 universe/finance/fundamental 各 **3** 次；
  共用一轮输入后各 **1** 次。本机（Windows / Python 3.14.5 / 中位数口径单次运行）：
  墙钟 **2.102 s → 1.215 s**、进程 CPU **2.047 s → 1.203 s**、tracemalloc 峰值 **20.80 MiB → 17.84 MiB**，
  即 −0.887 s / −0.844 s CPU / −2.96 MiB，且把哈希与深拷贝开销算在内。结果等价由三份 frame 的 SHA256 全部相等来断言，
  另有三个失效反例（重复读复用、空读不进缓存、消费者改写不影响下一个）。**限制**：合成输入、无行情网络、
  无数据库与盘中竞争，不能当成生产线整轮提速或收益。
- 2026-10-27（手单能力桥）：`manual_orders` 不再经 `paper_trading` 的全局桥取模块对象 ——
  `decision_engine` / `portfolio_allocation_policy` / `portfolio_allocation_weights` /
  `portfolio_runtime` / `portfolio_runtime_service` / `runtime_settings` 与
  `ReservationCycleMismatch` 改为直接依赖各自 owner，能力桥字段 57 → 50。业务依赖没有增加，
  但同一条依赖现在由两个消费者各自声明，`跨域 import 边` 661 → 666（额度 +5，已登记）。
  新增守卫：桥里不允许出现任何模块对象（带合成样本的非空性用例），且
  `import manual_orders` 不得把 `paper_trading` 拉进 `sys.modules`。
- 2026-10-27（闭环覆盖口径 / 退役文案观察器）：闭环准入台的「成交覆盖」分母改为后端 owner 字段 `closed_loop.strategy_coverage_total`（由 `_weight_participants()` 的周期参与快照派生，拿不到报 `null` 而不是 0），前端不再写死 `/ 5 当前账户`；同一份 overview 里参与集合只读一次，覆盖分母与 horizon 汇总共用同一快照。删除 `boot.js` 的 `MutationObserver` 整树文案改写与 `strategies.js` 的 `normalizeActiveStrategyCopy`，改为机械守卫：生产 Python 非 docstring 字符串常量、前端去掉注释后的字面量都不允许再出现退役称谓（`test_retired_strategy_copy_guard`，含可驱动成 RED 的非空性用例）。本切片不改变导入环、裸 SQL、私有访问、最大文件/函数等计数，因此不调整任何预算额度。
- 2026-10-27（第三批）：选股流水线从 HTTP 主入口下沉到 `selection_application`，API 与两个命令行入口共用；共享资金读取改为显式周期 owner；前端提供商设置不再反向导入设置页面。导入环 3 → 2，主文件行数降至 15,676，私有跨模块访问收紧到基线 202。缺报价账户/资金池/风险页保持 `unknown`，已实现盈亏仍可展示。
- 2026-10-27（前端加载/候选/选股复用）：`navigation` 不再导入任何 feature，页面 loader 由 `boot` 组合根注册；非选股深链不再无条件请求选股数据。前端静态环 1 → 0；单 bundle 体积基本不变，因此不把去环当成 JS 传输缩短。候选准备新增按 `(code, asof)` 的披露证据轮内复用，失败不缓存、不同日期重取，原有来源错误语义保持不变。`paper_selection.run_daily` 与定时 `selection_runner` 在一个显式 `selection_round` 内共享 live snapshot、snap 和 fundamental 计算；单策略调用不改变原路径。
- 2026-10-27：将无法证明来源的观测类型常量归到已有 `point_in_time` 契约；摄取层保留原名别名，观察账本不再反向导入摄取层。进化版本和全局参数边界归到共享契约，激活校验不再反导生成器。导入环从 5 组降为 3 组，守卫同步收紧；来源指纹、参数边界和事务语义不变。
- 建立可执行基线；历史预算采用本分支实测值，而非沿用旧报告数字。
- `paper_account_metrics` 承接账户绩效读投影；facade 保留 `_account_metrics` 导出及原签名。
- 外部事实通过冻结的 `AccountMetricDependencies` 晚绑定，不反向 import `paper_trading`，不冻结可变运行时依赖，不改变 SQL 和事务边界。
- 保留 `_legacy_account_metrics` 作为本 PR 的逐字段等价测试 oracle；后续去除 oracle 时可物理删除旧实现。

## 回归性能

共享 AST 缓存与单遍守卫索引独立于业务解耦。现在所有修改过的守卫都通过
`backend/test_support/ast_cache.py` 共享同一份「路径 + mtime_ns + size」快照，
文件变化会自然失效；缓存树是只读的，需要改树的测试必须先 `copy.deepcopy`。

`test_tradability_architecture_guard` 的原始读取守卫原先对每个函数重新
`ast.walk` 整棵树（O(函数数 × 节点数)），现为单次模块遍历，同时保留同名函数的
`lineno` 身份和逐函数例外。实测该用例：

| 版本 | 耗时 |
|---|---:|
| PR272 HEAD | 42.94 s |
| 缓存 + 单遍 | 6.05 s |

节省约 36.9 s（-85.9%）。这是守卫扫描的收益，不代表 94% 的业务链路测试变快；
完整业务链路测试仍必须执行，不以 mock 取代真实下单、事务和故障恢复用例。

## 后续纪律

1. 每搬一个簇，先列全局状态、连接归属与调用次数，再做等价迁移。
2. 表写入只归 owner；读投影走 read model，不通过拆文件复制裸 SQL。
3. 公共 helper 先建立语义矩阵。尤其 `None`、空串、布尔、NaN 的处理不同，不能因同名就统一。
4. 收紧预算时更新本表和测试；禁止为了提高阈值而修改基线。

## 2026-10-27 切片：测试与盘中扫描

本次只做两件结构性的事，并各自带可验证的量化结果。

### 1. 共享 AST 缓存的覆盖扩展

`backend/test_support/ast_cache.py` 已由上一轮建立（只读缓存树 + 按
`路径 + mtime_ns + size` 失效）。本次把仍在自己 `ast.parse` 的守卫接进来：

- `backend/tools/coupling_metrics.py` 的 `_trees()` 改为优先走缓存，
  且保留"缓存不可用时退回直接解析"，使 `tools/` 仍能脱离测试环境单独运行。
- `backend/test_execution_verification_wiring.py` 的 `_production_sources()`
  改为进程内读取一次 + 走缓存解析（它原本每用例重读重解析全部生产模块）。

效果（同进程内多守卫连跑，缓存才有意义）：`paper_trading.py` 单次解析约
0.50 秒，`test_paper_trading_architecture_guard` 由 18.1 秒降到 0.90 秒。

需要注意的口径：**单独跑某一个守卫看不到收益**——冷启动仍要解析全部生产
模块，收益来自"后续守卫直接命中缓存"。

### 2. 从 `paper_trading.py` 抽出负面公告策略

新增 `backend/news_risk_policy.py`：负面关键词分级、未核验缩量、数据源过期
与市场灯统一在 `decide()` 里判定**一次**。抽取同时修掉一个潜在双重扣减
（0.5 × 0.75 → 0.28）：原实现把系数分散在两处，抽出后只有一处。

预算影响与本次调整（最小够用，不重设基线）：

| 指标 | 基线 | 本次调整前额度 | 本次额度 | 实测 |
|---|---:|---:|---:|---:|
| 最大文件行数 | 16367 | +72 = 16439 | **+90 = 16457** | 16454 |
| 跨域 import 边 | 624 | +25 = 649 | **+28 = 652** | 650 |

增量来源：`news_risk_policy`（新模块，+1 跨域边）、`paper_trading` 内的行情
轮内复用与时段余量说明（原地增量）。抽出的约 60 行已从 `paper_trading.py`
移走，因此净增量远小于增加的功能量。长期目标不变（最大文件 ≤2500 行）。

## 2026-10-27 追加：AST 缓存不要"机械全量接入"（含一处推导更正）

`backend/test_support/ast_cache.py` 建的是**只读语法树**（每个节点带包装）。
2026-10-27 试过把 21 个守卫模块里 39 处内联 `ast.parse(x.read_text())` 机械替换
为 `ast_cache.tree(x)`，**实测更慢，已回滚**。

### 单文件成本（本机，`paper_trading.py`，1.6 万行 / 约 10 万 AST 节点）

| 操作 | 耗时 |
|---|---:|
| `ast.parse(source)` | **453.5 ms** |
| `ast_cache.tree(path)` 首次（建只读树） | **1412.4 ms** |
| `ast_cache.tree(path)` 命中 | 0.4 ms |
| 再 `ast.parse(source)` 一次 | 449.7 ms |

**回本点 ≈ 3 次命中**：一次命中省下的是"少解析一次"≈ `449.7 − 0.4 = 449.3 ms`，
而首建多付 `1412.4 − 453.5 ≈ 959 ms`，`959 / 449.3 ≈ 2.1`。

> 更正：本节初稿把这个回本点误算成 2341 次（错用命中成本 0.4 ms 当每次节省额），
> 并据此推出"只有同模块内复用才划算"。**该推导作废**——`ast_cache._CACHE` 是
> 进程级全局，跨模块命中同样计数，单文件只要被解析 3 次以上就能回本。

### 为什么接了缓存还是变慢（顺序受控的 A/B）

同一组 21 个模块、968 个用例，各跑两遍取第二遍（消除页缓存冷热差异）：

| | 耗时 |
|---|---:|
| 接缓存 | **62.05 s** |
| 不接缓存 | **54.76 s** |

接缓存慢约 **13%**。同一次运行里缓存统计为 `tree_calls=1621`、
`distinct_files=308`——平均每文件 5.3 次，**已超过 3 次回本点**。因此"命中太少"
解释不了这个结果；更可能的原因是**首建成本按文件数量与规模累加**：这批守卫会
遍历 308 个文件（含大量中小文件），每个文件都要付一次只读包装，而节省集中在
少数大文件上，总和仍是净支出。**这条是推断，未逐文件归因**，仅记录实测事实。

### 实际规则

* **不要**为了"统一"把缓存机械接到所有 `ast.parse` 上；这类改动必须带 A/B 实测。
* 已知有效的场景是**同一模块内反复解析同一个大文件**：
  `test_paper_trading_architecture_guard` 由 18.1 s 降到 0.90 s 属这类。
* 判断某模块该不该接：先数它解析同一文件的次数（≥3 次才可能回本），再看该文件
  的规模（越大越值得）。
* 若未来需要真正跨模块共享，应当缓存**源码文本/字节**而不是只读语法树，避免为
  每个 AST 节点付包装成本。

## 2026-10-28：行情源预算与低频状态缓存

- 新增基础模块 `env_utils`（`env_float` / `env_int`），替换 5 个模块里各自
  复制粘贴的环境变量解析。`duplicate_helpers` 314 → **313**（回到 313 额度内）。
- `data_fetcher`（data 域）与 `paper_live_tradability`（paper 域）各新增一条
  指向 `env_utils`（other 域）的边，`cross_domain_edges` 664 → **666**；
  `marketdata_transport` / `marketdata_feeds` / `alt_data` 也在 other 域，不新增边。
- 低频状态改为**跨进程**缓存后，`paper_live_tradability` 再新增 `data_paths`
  （paper → data）与 `marketdata_cache`（paper → other）两条边，666 → **668**。
  额度 40 → 44 并在此记录：这是共享实现替代复制，不是新增业务耦合。
- 缓存目录改为在 `_get` / `fetch_company_announcements` 内懒创建，避免 import
  期副作用（`import_side_effects` 保持 25）。
- 低频状态缓存窗口（停牌 15 分钟、公告 30 分钟、快讯 5 分钟）是运行时策略，
  不改变模块依赖。

## 2026-10-28 A1：vertical 依赖下沉 + 度量口径修正

### 依赖字段（三个 vertical 的 `*Dependencies` dataclass）

| 批次 | manual_orders | dashboard_queries | execution_planner | 合计 |
|---|---:|---:|---:|---:|
| 起点 | 53 | 23 | 16 | 92 |
| phase 1（纯工具直连 owner） | 48 | 21 | 13 | 82 |
| phase 2（常量归位） | 43 | 21 | 13 | 77 |
| phase 3（行读取/K 线/持仓天数归位） | 38 | 19 | 13 | **70** |

迁移的内容都是**已有 owner 的能力**，不是新实现：JSON/数值 → `paper_common` / `paper_numeric`；
状态词表 → `entry_lifecycle`；整手数量 → `paper_trading_rules`；共享池敞口 → `paper_shared_cash`；
行读取 → `paper_repository`；完整日线截断 → `data_fetcher.completed_kline`；持仓天数 → `paper_risk_evidence.hold_days`。
时钟（`_now` / `_date`）与数据库连接仍作为**可替换的窄能力**注入：回放与测试需要固定时点与固定账本。

### 度量口径修正

`coupling_metrics.domain_of` 之前按文件名前缀判定业务域，于是
`paper_repository` / `paper_trading_rules` / `paper_risk_evidence` /
`paper_position_read_model` / `paper_decision_audit` / `entry_lifecycle` /
`paper_common` / `paper_numeric` / `paper_storage` / `data_paths` / `env_utils`
这些**事实与基础 owner**被算进 "paper" 域。结果是：业务域模块直接 import owner
（A1 想要的方向）反而显示"跨域边上涨"。

现在这些模块单列为 `foundation`，与 foundation 相关的边不计入业务域跨域边
（foundation 反向依赖业务域由 import_cycles / private_cross_access 覆盖）。
实测跨域边 624（改口径前的基线）→ **613**，import 环保持 **0**，duplicate_helpers 312。
预算基线按实测收紧为 613，额度 4。

## 2026-10-28 C2 第三批：策略工坊撤全局桥

- 策略工坊的静态筛选、新建、搜索及动态卡片、编辑器、空态、生命周期和提案按钮，
  改为 `data-wb-*` 属性与工坊容器内的事件委托；启动入口只安装一次监听。
  不改变策略定义、生命周期规则、接口、确认流程或交易行为。
- `frontend/src/bridge.js` 的全局赋值按 `^window\.\w+\s*=` 实数统计，
  **111 → 86**；移除工坊 25 个桥（含 `loadStrategyWorkbench`），不是报告估算的 27 个。
  后端耦合预算不变。进化中心和设置页留待后续逐页处理。
- 同批更新 `bridge-contract.spec.js`：工坊必须无内联事件属性、无 `wb*` 全局桥，
  且动作集合非空；真实点击验证筛选、搜索、动态空态、新建、条件增删、模式切换，
  以及提案目标状态/指纹与删除确认。设置中心继续保留现有内联桥契约。
- 验证：`npm run build` 成功；`npm run test:unit` **251/251**；
  `pytest backend/test_frontend_module_contract.py -q` **12/12**；
  相关浏览器回归 17 项既有旅程通过，更新后的动作契约 **6/6** 通过（合计 23 项）。
  桌面与移动端 390 / 768 / 1024 / 1440px 布局检查通过；测试使用临时离线账本。
  首轮新增契约中两项未先填入有效 DSL，修正测试输入后复验通过；未放宽断言或浏览器错误检查。
