# PR-9 依赖边界收尾评估

基线：`master` @ `791680d`（#307 合并后）。范围：`dynamic_name_bridges` 残余 72 处的
逐点分类与治理建议。**本评估不修改生产代码。**

## 1. 口径（先对齐，否则会把结构性内省误判成依赖）

`coupling_metrics.dynamic_name_bridges` 统计三类写法：

| 形态 | 现在 |
| --- | --- |
| `globals()[...]` | **0** |
| `sys.modules[...]` | **0** |
| `getattr(obj, <非字面量名，或以下划线开头的字面量名>)` | **72** |

这条口径是**过近似**：它无法区分"按名字访问另一个模块的私有成员"和"按字段名遍历自己的
dataclass"。所以评估的做法是逐点看 `base`（被访问对象）与 `name`（名字来源）：
**只有当 base 是另一个模块、或 name 来自外部输入/开放集合时，才算真实跨模块动态依赖。**

## 2. 72 处的分类结果

| 类别 | 数量 | 判定 |
| --- | --- | --- |
| A 构造期字段规范化（`__post_init__` / `__init__` 按声明字段循环） | **43** | 合法内省 |
| E 契约/行字段拷贝与常量集合（投影→dict、DB 行→dict、字面量元组比较） | **26** | 合法内省 |
| C 模块/函数身份检查（`getattr(value, "__name__")` 做禁止依赖守卫） | **2** | 合法内省 |
| B 兼容代理转发（`__getattr__` 委托给内部 plan） | **1** | 兼容层，保留 |
| D 组装根 `globals()` / `sys.modules[]` | **0** | 已归零 |

**真实跨模块私有动态依赖：0 处。**

### 2.1 A 类（43）分布

`experiment_contract` 8、`learning_evaluation` 6、`candidate_selection` 5、
`earnings_disclosure_facts` 4、`adaptive_risk` 2、`adaptive_selection` 2、`shadow_comparison` 2，
其余 14 处各 1（`ai_capability`、`earnings_disclosure_ledger`、`execution_evidence`、
`execution_verification`、`experiment_search_contract`、`native_paired_replay`、`news_learning`、
`paper_runtime_facts`、`portfolio_allocation_policy`、`robustness_contract`、`strategy_candidate`、
`strategy_candidate_search_space`、`strategy_promotion`、`walk_forward_validation`）。

形态统一是：

```python
def __post_init__(self):
    for name in SOME_DECLARED_FIELDS:      # 模块常量 / dataclasses.fields(self)
        value = getattr(self, name)        # ← base 是自己
```

`base` 全部是 `self`，`name` 全部来自本模块声明的字段列表或 `dataclasses.fields(type(self))`。
改成显式代码只会把"一份字段表 + 一个循环"复制成 30 段重复赋值，**风险大于收益**。

### 2.2 E 类（26）分布

`candidate_selection` 4、`execution_planner` 3、`robustness_contract` 3、
`tradability_observation_ledger` 2，其余 14 处各 1。代表站点与判定：

| 站点 | 形态 | 判定 |
| --- | --- | --- |
| `execution_verification._row_field` / `tradability_position_evidence._row_field` | `getattr(row, name)`（sqlite3.Row 列名） | 行→dict 映射，合法 |
| `strategy_selection_provenance._row_get` / `tradability_observation_ledger._archive_row_identity` | 同上（`key` 来自声明的列集合） | 合法 |
| `deepseek_research._execution_leg` | `getattr(projection, name)`，`name ∈ EV.EXECUTION_FACTUAL_FIELDS` | 契约常量集合，合法 |
| `execution_planner.L399` | `getattr(reading, field)`，`field` 是字面量四元组 | 合法 |
| `execution_planner.L785` | `getattr(tradability, "buy_block_reason" if side == "buy" else "sell_block_reason")` | 两个公开常量名，合法 |
| `manual_orders._terminalize_cycle_stale_order` | `getattr(exc, field, None)`，四个字面量属性 | 异常属性探测，合法 |
| `shadow_runtime._captured_entry_state_projection` | `getattr(state, item.name)`（dataclass 字段） | 合法 |
| `paper_risk_evidence._position_alt_signals` | `getattr(alt_data, name)`，`name ∈ _POSITION_ALT_SIGNALS`（模块常量） | **闭合集合的鸭子类型端口**，见 §3 |
| `experiment_search_contract.__getattr__`（B 类） | 委托给 `self.plan` 的 v1 兼容面 | **兼容层**，保留 |

### 2.3 C 类（2）

`learning_dataset.forbidden_dependencies` 与 `learning_evaluation.forbidden_dependencies`：
`getattr(value, "__name__")` 取被检查对象的名字，用于**禁止依赖**守卫。这是守卫本身的实现，
不是依赖。

## 3. 需要单独记录、但不建议改的 4 个灰区

| 站点 | 为什么灰 | 结论 |
| --- | --- | --- |
| `paper_risk_evidence._position_alt_signals` | 按名字调用**注入的** `alt_data` 上的 6 个可选信号函数 | 名字集合是模块常量 `_POSITION_ALT_SIGNALS`，且每个 source 独立失败降级；改成显式 dict 只是把常量搬到注入点，收益为零。**保留**，并在 docstring 里注明"名字集闭合、失败按 source 隔离" |
| `experiment_search_contract.__getattr__` | v1 消费者按旧字段名访问 v2 plan | 这是有意的兼容门面；删掉会破坏 v1 调用方。**保留**，等 v1 消费者清零再删 |
| `execution_planner.L399` / `L785` | 常量集合与三元常量名 | 不构成跨模块依赖。**保留** |
| `manual_orders` 的异常属性探测 | 从异常对象上取四个周期字段生成审计 detail | 失败安全（`None` 跳过）。**保留** |

## 4. 如果要继续降指标：逐簇方案与代价

只有把"合法内省"重写成显式代码才会动这个数字，按成本排序：

| 方案 | 覆盖 | 预计 | 代价 / 风险 |
| --- | --- | --- | --- |
| 把 `__post_init__` 的字段循环展开成显式赋值 | A 43 | 72 → 29 | 30+ 处重复代码，新增字段时容易漏写；与"单一字段表"的初衷相反 |
| 投影/行拷贝改成 `dataclasses.asdict` + 白名单 | E 26 | 72 → 46（再 −26） | 行对象不是 dataclass；会引入第二套序列化规则 |
| 兼容代理 `__getattr__` 删除 | B 1 | −1 | 破坏 v1 调用方 |
| 身份检查改成显式类型判断 | C 2 | −2 | 守卫语义变弱（`__name__` 检查是刻意的） |

**建议：不做。** 这些改动的收益只是指标好看，代价是可读性、字段表单一性和兼容性。
PR-9 的验收写的是"dynamic bridges 持续下降"，80 → 72 已经成立，且下降来自**真实的依赖桥**
（组装根 `globals()` 与 `main` 的私有 `getattr`），不是数字游戏。

## 5. 可选守卫（本次不实施，留给后续批次决定）

现在 `globals()` / `sys.modules[]` 已是 0，可以加一条**窄守卫**锁定这个性质：

> 禁止对**模块对象**做动态成员访问：`getattr(<import 别名>, <非字面量名>)`。

实现要点（不新增框架，复用 `coupling_metrics` 的一趟 AST 走查）：

1. 收集本模块的 import 别名集合（`import x as y` / `import x`）；
2. 对 `getattr(base, name)`，若 `base` 是 `Name` 且在该集合里、`name` 非字面量 → 计数；
3. 预算设为 0。

当前实测该形态为 **0 处**，加上即可"锁定"而不是"新增约束"。之所以不放进本评估：
它会给 PR-10 的账本 owner 化增加一个无关的守卫改动，建议单独一个极小 PR。

## 6. 结论

1. 残余 72 处**全部**是本模块数据结构的运行时字段内省 + 1 处兼容代理；没有一处通过名字访问
   另一个模块的成员。
2. 真实依赖桥（`globals()` / `sys.modules[]` / 模块对象的动态 getattr）已归零。
3. 建议 **PR-9 收尾**，不为指标归零重写内省；如需锁定性质，另开一个"模块对象动态访问"窄守卫的
   小 PR。
4. 下一步按路线图进入 **PR-10：核心账本表 read/write owner 化**。

MERGE: NOT MERGED · DEPLOY: NOT DEPLOYED
