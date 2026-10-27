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
