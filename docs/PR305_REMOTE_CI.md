# PR305 远端 12 条失败调查

审核提交 C：`623d87f`（完整 SHA 见 git 历史）。
唯一原始基准 A：`e622955`；历史实现 B：`bdc9530`（均为短 SHA，完整值见 git 历史）。

## 远端事实与根因

[失败 CI 41200405130](https://github.com/daviesjoin-afk/astock-paper-trading/actions/runs/41200405130)
的 tests job `124534913791` 使用 Python 3.14.9，实际是 **9 observations + 3 exception_audit**，
共 12 条子断言失败。该提交的 Docker job 使用 3.14.10、`TZ=Asia/Shanghai`，7,593 项通过；
Security Leak Scan 通过。

根因是测试漏冻 **独立的 `execution_dispatch._now()`**。原 oracle 在闭市时记录
`next_window=None`；UTC runner 在 2026-10-31 12:17 采集到：

```json
{"index":0,"start":"14:30","end":"14:45","starts_at":"2026-10-31T14:30:00","ends_at":"2026-10-31T14:45:00"}
```

这段描述性窗口数据进入 slice 前的观察，且在三类异常现场仍留在 risk 字典中。
它不是数量、对象身份、分片计划、异常来源或交易结果变化。`PT._now()` 和分配 fixture
decision_at 的冻结不能替代派单模块自己的时钟。

## 逐条失败核对

以下 12 条与远端失败记录一一对应。O 路径为
`observations.slice.execution_dispatch.next_window`；E 路径为
`exception_audit.execution_dispatch.next_window`。每条都只有此叶字段从 `None` 变成上面的字典。
表中数量是进入 slice 时的 caller-local qty；B 在这个提前调用点尚未绑定局部 qty。

| # | 场景 | 维度 | A qty | B qty | C qty | 现场/最终结果（A/B/C 的交易投影相同） |
| --- | --- | --- | ---: | --- | ---: | --- |
| 1 | single | O | 19900 | 未绑定 | 19900 | 成交 19900，无分片状态 |
| 2 | new_slices | O | 19900 | 未绑定 | 19900 | plan=[6600,6600,6700]，filled=1，成交6600 |
| 3 | continuation | O | 19900 | 未绑定 | 19900 | plan=[100,200,300]，filled=1，qty200 延期、未成交 |
| 4 | zero_slice_target | O | 19900 | 未绑定 | 19900 | plan=[0,200,300]，filled=1，原零目标回退行为保留，成交19900 |
| 5 | trial_first_cap | O | 3900 | 未绑定 | 3900 | 首仓后3900；本片1300，延期、未成交 |
| 6 | dispatch_block_price_expired | O | 19900 | 未绑定 | 19900 | 本片6600，expired，原理由顺序保留，无成交 |
| 7 | slice_builder_error | O | 19900 | 未绑定 | 19900 | StageFailure(plan_builder)，无新增订单/成交 |
| 8 | slice_builder_error | E | 19900 | 未绑定 | 19900 | 异常 caller qty19900；分片未返回，事务回滚 |
| 9 | malformed_plan | O | 19900 | 未绑定 | 19900 | ValueError：prepare_entry_slice 内 int("bad") |
| 10 | malformed_plan | E | 19900 | 未绑定 | 19900 | 异常 caller qty19900，坏 plan 原样保留，事务回滚 |
| 11 | deployment_setting_error | O | 400 | 未绑定 | 400 | slice 后 qty100，随后 theme_setting 故障 |
| 12 | deployment_setting_error | E | 400 | 未绑定 | 400 | 异常 caller qty100；局部 plan=[100,100,200]，未提交/未应用部署 cap |

### 每个场景共同检查的阶段字段与身份

普通 A/C slice 现场的 `sizing` 阶段字段：target_amount、entry_tranche_scale_pct、
minimum_order_amount、execution_limit_price。trial_first_cap 另外包含
entry_tranche_qty 和 main_force_first_tranche。deployment_setting_error 的异常现场
还包含 entry_slice_target_qty/index/total；此时部署 cap 审计尚未出现。
这些字段的值与原 oracle 逐项相等，没有排除任何失败字段。

对上表每一个 slice 现场，A 的 `local sizing is risk.sizing=True`，但没有
EntryQuantityResult；C 的 local sizing / risk.sizing / EntryQuantityResult.sizing
是同一个对象。B 的提前 slice 现场三者尚未附着于 caller。A/C 的 risk 字段包含
capital_deployment、entry_opportunity、exceptional_opportunity、execution_dispatch、
execution_profile、sizing；价格等待过期场景另含 entry_price_wait。部署前设置故障的
异常现场另含 entry_slices。只有窗口值随未冻结的时钟变化，身份和其余字段没有变化。

A/C 顺序：`base → profile → price_wait → dispatch → slice → [plan_builder]`。
B 顺序：`base → slice → [plan_builder] → profile → price_wait → dispatch`。
仅新建分片会调用 builder；single/continuation/zero target/坏 plan 不会新建计划。
B 在 builder/坏 plan 处抛错时止于提前 slice，A/C 已先经过 dispatch。
异常来源分别为注入的 plan_builder、prepare_entry_slice 的转换、注入的 theme_setting；
这些来源在同一输入下与各自已有基准行为一致。A 的 prepare_entry_slice 位于
paper_entry_decision，B/C 位于 paper_sizing，这是既有 owner 迁移，不是本轮异常变化。

完整逐场景输出保留事件、局部数量、完整 sizing 字典、三者身份、risk 字段、
slice_state/detail、EntryQuantityResult 的分片字段、异常类型/函数/注入阶段、
异常现场和最终交易投影；现场文件为 `.codex-test-tmp/pr305-ci/ABC-CI-comparison.json`。
其中所有 C_vs_A 均为空；B 的历史阶段差异仍如实保留。

## 四类差异分开归因

1. **生产行为**：同一派单时钟下，A/C 的16场景所有既定维度一致；本轮不改生产代码。
   B 的历史提前分片差异真实存在，不能被本次时钟问题抹去。
2. **局部赋值时序**：A/C 的局部 qty、sizing 身份、risk 附着和分片赋值时点一致；
   B 提前 slice 时 caller qty 未绑定属于旧问题，不新增“忽略局部差异”的豁免。
3. **探针输入/采集**：业务阶段采集点未移动，但派单审计依赖的独立墙钟未冻结，
   导致在正确阶段采集到不同窗口事实。这是本轮12条失败的原因。
4. **Python 版本**：3.14.5 和 3.14.9 在相同 12:17 输入下均复现同12条；
   改为闭市输入后两者均通过。没有证据把本轮失败归为 Python 补丁版本语义差异。
   本地3.14.9对照共用原3.14依赖目录（pandas3.0.5），远端最终用 requirements.lock 单独验收。

## 最小修复与防回退

只修改测试：显式冻结独立派单时钟为基准既定的闭市状态（DAY 15:30）；
保留所有原16场景、6维逐值断言和原33组冻结输入。**两个 oracle 文件均未修改**。

另加回归：正常、builder异常、部署前设置异常分别在12:17和15:30运行。
验证有未来窗口时必须保留窗口及来源日期，异常现场必须保留同一派单审计，
外部环境时钟不得泄漏，事件/异常/最终返回/交易投影仍保持既定契约。
没有把 next_window 删除、归零或从比较中移除。

oracle git blob 的短标识为 `7614f9c1c26b`；原33样本为 `0600c197886a`。
完整标识可用 `git hash-object backend/fixtures/entry_sizing_stage_order.json` 和
`git hash-object backend/fixtures/entry_quantity_frozen.json` 核对；两个文件相对623d87f均零diff。

本地3.14.9：专项3项（16场景及额外6组时钟组合）通过；原样本与自动/手动/
分片/planner/OrderIntent 共163项通过；架构/import laziness/coupling 共123项通过；
Ruff、compileall、diff check通过。3.14.5专项也通过。
本轮无生产改动，按要求不额外执行本地完整L4；完整后端由新 exact HEAD 的远端CI验收。
最终远端运行链接与结果记入 PR 描述，全部全绿后才交付；保持Draft，不合并不部署。
