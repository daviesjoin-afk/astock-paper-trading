# 架构守卫治理规则 v2

日期：2026-11-04  
基线：`dad42e5a8924f5465c86dbf00efa20115305c597`  
适用范围：整个 backend、frontend、部署调度、数据与研究系统。

## 1. 核心原则

**守卫保护业务正确性，不保护旧目录结构。**

本项目允许重设模块、合并服务、删除流程、替换 scheduler、调整依赖方向和移动文件。任何历史 guard 都不能单独成为保留旧模块、旧 wrapper 或旧调用链的理由。

守卫分为四类：

| 类别 | 处理方式 | 例子 |
|---|---|---|
| G0 业务不变量 | CI hard fail | 账本一致性、唯一成交提交、T+1、风险退出、PIT、周期身份、任务 fencing、AI 授权、操作员鉴权 |
| G1 能力边界 | CI hard fail，但 owner 可迁移 | 单一写入 authority、行情 provider 边界、只读查询、研究证据签发、参数应用审批 |
| G2 架构形状 | 报告和 PR review，不设绝对上限 | LOC、函数长度、模块数、fan-out、跨域边数、raw SQL 模块数、重复 helper、延迟 import 数 |
| G3 迁移期守卫 | 有条件保留，必须可退出 | `paper_trading.py` 兼容入口、指定 wrapper 名称、旧模块 import allowlist、旧路径禁止回流 |

G0/G1 保护的是“系统必须做到什么”，不是“必须由哪个旧文件做到”。

## 2. 必须保留的业务不变量

以下规则不能因为重构而删除。可以更换实现者、模块名和调用方式，但必须由新的等价行为测试接管。

### 交易与账本
- 一笔模拟成交只能有一个提交 authority；现金、订单、预占、fills、lots 和审计在正确事务边界内一致。
- 成交幂等；部分成交、重试、崩溃恢复不得制造重复经济事实。
- T+1、整手、停牌、涨跌停、交易时段、流动性和报价新鲜度约束继续生效。
- 风险退出不得被候选扫描、AI、Dashboard 或研究任务阻塞。
- 历史账本和审计事实不可因删除任务或迁移模块而被清除、伪造或用当前行情回填。

### 身份与时间
- 交易决策、订单、成交、持仓、信号和策略版本必须绑定明确的 cycle / as-of / immutable version。
- 历史读取不得重新解析 current cycle、current head 或墙上时钟来补造身份。
- PIT、样本成熟度、OOS、Robustness 和数据可用时点必须有明确证据。

### 运行与授权
- 持久任务的 lease、heartbeat、fencing token、CAS、幂等和故障恢复必须保持。
- provider 网络请求不得持有 SQLite 写事务。
- AI 研究/审核仍是 advisory；正式参数应用和策略晋级必须走授权、版本和回滚门禁。
- 操作员鉴权、Origin/CSRF、密钥隔离和远端访问限制必须保持。

## 3. 不得作为 CI 硬门槛的指标

以下指标保留测量价值，但不能单独阻止合理架构调整：

- 单文件 LOC、最大函数 LOC、模块级函数数。
- backend 模块总数、目录层级、文件名是否带后缀。
- 跨域 import edge 总数、raw SQL 模块数、paper 表 fan-out 总数。
- duplicate helper 名称数量、延迟 import 数、动态属性访问总数。
- facade / service 的固定行数上限。
- 某个特定旧文件必须存在、某个特定 wrapper 必须保留。

原因：这些指标是复杂度代理，不是业务正确性的充分条件。合法拆分可能暂时增加模块数、显式依赖或 owner 边；反过来，减少 LOC 也可能只是增加 wrapper 和跳转。

当前 `test_architecture_boundary_audit.py` 已调整为：导入环和基础层反向依赖继续 hard fail；其他结构指标只要求可测量，不设置数值天花板。历史 `coupling_budget.lock.json` 保留为比较快照，不再是 CI ceiling。

`test_paper_trading_architecture_guard.py` 中的 risk facade 40 行上限和 risk service 925 行上限已移除。不能再通过机械拆函数来满足 LOC 阈值。

## 4. 现有守卫的具体审核结论

### 保留为语义守卫
- 唯一成交提交、paper_fills 单写入 authority、source_fill lineage。
- cycle/as-of/version provenance，不允许历史 current-fill。
- 风控决策、卖出收尾、订单预占和成交事务一致性。
- Market Data provider authority、TradabilityArchive、PIT 和 freshness。
- AI provider transport、typed research evidence owner、canonical research 单写入。
- Learning canonical partition、operator security、deployment profile。

### 需要随新架构重写，不得原样限制新模块
- `DOMAIN_MODULES` 的固定文件名清单。
- `paper_trading.py`、`execution_planner.py` 等固定路径/函数名断言。
- 纯模块的零项目 import 规则：只对明确声明为 pure contract / pure decision 的模块适用，不应套在 application service 或 infrastructure adapter 上。
- risk facade 的“必须存在且必须委托旧 service”形状。
- 任何要求某业务能力必须位于某个历史文件的 guard。

这些守卫迁移时应改成按 capability / behavior 验证，而不是删除业务不变量。

## 5. Guard 修改协议

修改或删除一个 guard 时，PR 必须说明：

1. 它防止的真实缺陷是什么；若没有历史缺陷或可说明的风险，解释保留价值。
2. 它属于 G0/G1/G2/G3 哪一类。
3. 新架构是否仍需要该不变量。
4. 若仍需要，新的 owner、契约和等价测试在哪里。
5. 若不再需要，为什么业务语义已经消失或被更高层机制替代。
6. 至少有一个负向控制，证明测试在注入真实违例时会失败。
7. 不能通过 broad allowlist、skip、xfail 或仅改测试断言来让 CI 变绿。

**禁止两种做法：**
- 为了让旧 guard 通过，保留一个本来应该删除的 facade / wrapper。
- 为了让新架构通过，直接删除唯一成交、PIT、风险或授权守卫而不提供替代验证。

## 6. 模块移动前的强制前置工作

当前多个守卫仍按 `backend/<filename>.py` 定位源码，且 `coupling_metrics.production_paths()` 使用顶层 `glob("*.py")`。如果直接把模块移动到子目录，部分守卫会报找不到旧文件，另一些扫描可能完全漏掉新包。

因此在 P2 批量移动模块前，必须完成：

1. 建立 capability → owner → source path / symbol 的架构登记表。
2. 所有生产源码扫描递归覆盖 backend 子目录，排除测试、生成物和工具脚本时必须显式登记排除理由。
3. import graph 支持 package-qualified module ID、相对导入和绝对导入。
4. 唯一写入者扫描必须递归覆盖新 package。
5. 增加负向测试：在临时子目录放入第二个 writer，守卫必须检测到。
6. 将依赖旧文件路径的 guard 改为读取 owner registry 或稳定 capability contract。
7. 文件移动、owner registry 更新和守卫迁移必须在同一个 PR 完成。

在上述工作完成前，可以先做模块职责迁移和小范围文件移动，但不得进行大规模目录重排，以免守卫产生盲区。

## 7. 结构指标如何使用

每个 PR 的结构指标报告应包含：
- 改造前后指标。
- 新增/删除的模块和依赖边。
- 为什么某项增加是合理的。
- 是否减少了业务跳转、重复事实或重复 authority。
- 对运行时延迟、资源、SQL、provider 请求的实际影响。

结构指标用于帮助 reviewer 判断取舍，不用于替代业务证据，也不要求每项指标每个 PR 都单调下降。

## 8. 本轮调整范围

本轮只调整守卫治理：
- 取消结构指标的 CI 数值硬上限。
- 保留 import cycle 与 foundation dependency direction hard gate。
- 删除两个固定 LOC / facade 大小门槛。
- 将 paper_fills 唯一 writer 扫描扩展到 backend 子目录。
- 暂不修改交易规则、调度行为、数据库 schema 或生产数据。

后续 P2 的首个工程任务是 package-aware guard scanner 与 capability owner registry。
