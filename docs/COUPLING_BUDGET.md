# 耦合预算与模块化进度

基线：PR272 HEAD `961c09f`。度量入口：`backend/tools/coupling_metrics.py`，由 `backend/test_architecture_boundary_audit.py` 执行不回退门禁。分析器只读 AST，不 import 生产模块。

## 当前预算

| 指标 | PR272 基线 | 本次收口后 | 允许增量 | 长期目标 |
|---|---:|---:|---:|---:|
| import 环 | 5 | 5 | 0 | 0 |
| 延迟 import | 583 | 626 | 43 | 逐步降低；仅移除无必要的延迟依赖 |
| 裸 SQL 模块 | 135 | 140 | 5 | ≤60 |
| paper 表最大模块 fan-out | 49 | 50 | 1 | ≤8（逐表 owner/read model） |
| 跨模块私有属性访问 | 202 | 205 | 3 | 0 |
| 跨域 import 边 | 624 | 649 | 25 | 分阶段降低 |
| 最大文件 | 16367 行 | 16439 行 | 72 | ≤2500 |
| 最大函数 | 1191 行 | 1196 行 | 5 | <200 |
| 单函数最大全局引用 | 56 | 57 | 1 | <80 |
| 重复顶层 helper 名 | 307 | 312 | 5 | <30 |
| `_num`/`_number`/`_finite` 重复定义 | 54 | 54 | 0 | 按语义逐模块收敛，不按名称批量替换 |
| 无归属前缀模块占比 | 41.39% | 40.72% | 0 | <30% |
| 顶层 import 副作用 | 25 | 25 | 0 | <5 |

允许增量来自本次审核修复必须新增的生产模块与调用点（`research_pipeline`、`research_candidate`、`research_orchestration`、`research_runtime_config`、`paper_account_metrics`）。允许增量是显式预算，不是重设基线；未来任何恶化仍会失败。`paper_trading.py` 只做了读模型外移，最大函数仍是 `_buy_order`，未做高风险重写。

## 本次切片

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

## 2026-10-27 追加：AST 缓存不要"机械全量接入"

`backend/test_support/ast_cache.py` 的只读树对**同一文件被反复解析**才划算。
2026-10-27 试过把 21 个守卫模块里 39 处内联 `ast.parse(x.read_text())` 机械
替换为 `ast_cache.tree(x)`，结果**更慢**，已回滚。

实测（本机，`paper_trading.py`，1.6 万行 / 约 10 万 AST 节点）：

| 操作 | 耗时 |
|---|---:|
| `ast.parse(source)` | **453.5 ms** |
| `ast_cache.tree(path)` 首次（建只读树） | **1412.4 ms** |
| `ast_cache.tree(path)` 命中 | 0.4 ms |
| 再 `ast.parse(source)` 一次 | 449.7 ms |

建只读树比直接解析贵约 **959 ms**，需要约 **2341 次命中**才回本。而"71 个守卫
各解析一次"最多只有 71 次——远够不到回本点。21 模块 A/B 实测：接缓存 64.00 s，
回滚后 58.70 s（**慢 9%**）。

结论与规则：

* 只有**同一模块内部**反复解析同一个大文件时才接缓存（例如
  `test_paper_trading_architecture_guard` 由 18.1 s 降到 0.90 s 属这类）。
* 一次性解析的守卫保持 `ast.parse`，不要为了"统一"而接入。
* 若未来确实需要跨模块共享，应当缓存**源码文本/字节**而不是只读语法树，
  避免为每个 AST 节点付包装成本。
