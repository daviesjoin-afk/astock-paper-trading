# PR-8 自动 / 手动买入数量公共化

基线：`master e622955deb702ae65e101ce260a0e3bea2e0a2f0`（PR-7 #304 已合并）。
路线依据：《模拟盘升级路线图》§七 PR-8；本文件调用图来自本轮源码审计。
MERGE: NOT MERGED；DEPLOY: NOT DEPLOYED。

## 修改前调用图

```text
自动信号 → OrderIntent 契约 → _buy_order
  → _price_aware_qty → paper_sizing.price_aware_qty
  → 调用方：试仓缩放、首仓金额上限
  → paper_entry_decision.prepare_entry_slice：分片
  → 调用方：deployment lots 上限、最低金额/费用/现金门禁
  → 订单 / 预占 → execution_planner.commit_fill

日内回补 / 确认加仓 → OrderIntent → _price_aware_qty
  → 调用方：已卖数量 / 现有持仓或净值比例上限 → 原有提交路径

手动 API → preview / confirm → manual_orders._manual_order_plan
  → 注入 _price_aware_qty → paper_sizing.price_aware_qty
  → 调用方：requested_qty 默认值、模型上限、正整手检查
  → 资格 / 行情 / cash_gate → 订单 / 预占
  → revalidate_order_plan（重建同一规划） → commit_fill
```

基础的风险/权重/行业/现金/剩余预算公式原本已经共用；问题是最终数量的
缩放、上限和请求校验仍散落在调用方，且手单经主引擎注入私有 sizing 能力。

| 差异 | 自动 | 手动 | 本轮处置 |
| --- | --- | --- | --- |
| 输入 | cycle-pinned 有效风险、实时执行价、流动性上限、执行缩放、分片与部署政策 | requested_qty、市价或限价参考价、原有风险画像与预算 | 显式传入，保留原值和数值顺序 |
| 业务资格 | 候选、OrderIntent、信号时效、模型、策略审批 | 授权、人工确认、账户、模型复核 | 保留各入口，不进入纯数量函数 |
| 数量 | 基础最小约束后再缩放、首仓、分片、部署上限 | 非正请求取推荐；正请求超限或非整手拒绝 | 全归现有 paper_sizing owner |
| 提交 | 信号关联、自动订单/风险审计 | 手动订单/授权与审计、待成交重验 | 保持原提交路径，共用原 planner 权威 |

自动的流动性输入和最低建仓金额门禁没有强加给手单；手单也不接受自动试仓政策。
这些是原有业务输入/资格差异，不是保留另一套数量公式。

## 修改后结构与契约

```text
自动 _buy_order ───────────────┐
手动 _manual_order_plan ───────┼→ paper_sizing.entry_quantity → EntryQuantityResult
回补/加仓 → 兼容 _price_aware_qty┘       │
                                       ├─ _risk_quantity（模块内私有基础公式）
                                       ├─ tranche / first cap
                                       ├─ prepare_entry_slice
                                       ├─ deployment / rebuy / add cap
                                       └─ requested quantity validation

各业务入口 → 原资格与最小金额 / 费用 / 现金门禁
           → 原订单 / reservation → planner revalidate / commit_fill
```

`entry_quantity` 只依赖标准库，不读数据库、行情、时钟或环境，不拥有事务。
它计算**委托建议数量**，不批准交易，也不承诺最终成交数量。

输入使用显式类型标注：净值、现金、持仓/行业/单票市值、执行价格、止损距离、
风险画像、策略/共享池额度、在途预占、流动性与绝对单票金额，以及可选的
requested_qty、tranche scale、首仓金额、分片请求、部署数量、回补/加仓上限。
`EntrySliceRequest` 只携带原分片所需的信号、账户、as-of、续片状态与生命周期
plan builder，不是全局 Context；它不包含资格判定、数据库连接或成交能力。

自动入口按原业务时点多次调用**同一个** `entry_quantity`：首次传基础数据，后续
首参传同一次尝试的 `EntryQuantityResult`，只应用本阶段的显式缩放/分片/部署输入。
后续调用复用 sizing 审计字典，不重算基础公式；金额阶段必须传原始精度的 fill_price。
不得跨订单、扫描尝试或重验复用前阶段结果。手单仍是一次完整调用。

`EntryQuantityResult`：

| 字段 | 契约 |
| --- | --- |
| qty | 委托提议；手动超限/非整手时仍保留请求值，必须结合 reasons，不得直接成交 |
| recommended_qty | 同一套风险和金额约束得出的可推荐数量 |
| sizing | 原有审计字典，逐字段保留阶段语义；target_amount / risk amount 并非最终成交值 |
| reasons | 仅数量错误，按原顺序排列；不混入业务资格、行情、最小金额或预占拒绝 |
| slice_state / slice_detail | 原分片状态与审计，只计算，不推进 filled，不修改原 payload |
| pre_slice_qty | 分片前数量兼容字段；主编排已恢复在原 exceptional 时点读取当阶段 qty |

计算顺序与旧代码一致：风险/权重/敞口/行业/现金/流动性取最小 → 整手向下取整
→ 试仓缩放 → 首仓金额 → 分片 → 部署上限 → 回补/加仓上限 → 手动请求校验。
调用方决定何时适用某项业务政策，只传入数值，不再复制这套数量运算。

**不能把上述算术顺序误写为连续无间隔执行**。自动路径中间仍有原业务门禁：
基础 sizing → 读取试仓输入/缩放 → 首仓限制 → exceptional_opportunity → price_wait
→ execution_dispatch → entry slices → 聚合设置读取/门禁 → deployment cap。

### 拒绝理由与兼容

- 无效价格：推荐量 0，原审计 `reason="无有效价格"`；额外覆盖 NaN、无穷和非数值，
  避免进入整数转换。常规价格输出完全不变。
- 非正手动请求：沿用自动推荐，包括历史上的负数默认行为；小数字段沿用旧 int 截断。
- 正请求超出推荐量：`委托数量超过模型上限 N 股`，保留原请求，不静默缩单。
- 非正整手：`委托数量必须为 100 股的正整数倍`；若同时超限，超限理由在前。
- 手动输入的历史严格数值转换继续保持：数字字符串和 NaN 仍按默认值处理。
  无穷请求现在明确拒绝，而非抛整数转换异常；不产生放行。
- `price_aware_qty(*args, **kwargs)` 保持 `(qty, sizing)` 返回，转发一次新入口。
  主引擎 `_price_aware_qty` 保留旧位置参数与运行时数值/整手依赖。
- `paper_entry_decision.prepare_entry_slice` 是 sizing 函数的同一对象别名，
  无重复函数体；生命周期的 `entry_slice_plan` 和状态推进仍归原 owner。

## 不变量与执行边界

不改策略评分、因子权重、候选顺序、风险阈值/减仓、分配结果、共享现金、席位容量、
T+1、整手、停牌/ST/退市/涨跌停、cycle、lease/fencing、手动授权或订单审计。
策略意图仍经过 OrderIntent 校验，策略声明数量仍被拒绝。

`reserve_shared_capital` 只创建/重算预占，不扣实际现金；跨周期 reserved BUY
继续占共享购买力，重验排除自身 order_key。`revalidate_order_plan` 继续从持久订单
取 remaining_qty、价格和身份，重建同一 plan，不缓存上轮数量结论。
`commit_fill` 仍按 lease → provenance → identity → execution-cycle → 执行模拟
→ 原子写入的顺序运行，按订单和当场证据重算实际价格、数量、费用与部分成交。
本 PR 不修改 execution_planner、reservation、allocation 或 lot owner 的实现。

## 冻结矩阵与复跑

`backend/fixtures/entry_quantity_frozen.json` 由合并后的 master、同一份
`scripts/pr8_sizing_probe.py` 和临时生产 schema 账本生成，没有用新函数作为旧行为 oracle。
基线文件保留全部选取的 sizing 审计字段和理由，不删减差异字段：

- 17 组基础约束：正常、零/负价格、整手上下界、零/负现金、涨价、风险、单票、行业、
  策略/共享池预占耗尽、流动性、绝对金额上限、执行缩放。
- 6 组真实自动 BUY：正常、报价变化、高价拒绝、首次分片、续片、策略 pick 越权数量。
- 10 组真实手单 requested_qty：0、负数、1、99、100、101、100.9、超大请求、字符串、None。

完整投影包括：数量/推荐量、订单状态/实际 filled_qty、理由顺序、是否 allowed、
自动返回结果及 sizing。未比较整个账本所有列；周期、预占、成交、T+1 由独立生产回归覆盖。
新增边界还覆盖最小金额 ±0.01/相等、席位耗尽、已有预占、无效价格/数量、旧入口、
planner 部分成交与 T+1/可卖量权威，并用 AST 守卫禁止最终数量计算回流。

```bash
# Windows 从已验证的 Git Bash 运行，Python 3.14.5。
python scripts/pr8_sizing_probe.py --output .codex-test-tmp/pr8/replay.json \
  --compare backend/fixtures/entry_quantity_frozen.json
PYTHONPATH=backend python -m unittest test_entry_quantity
python scripts/run_tests_parallel.py --shards 8 --timeout 900
```

## 分层验证与性能

探针已加入 CI Ruff 范围。以下为首次提交 `bdc9530` 的 Python 3.14.5 验证记录，
本次阶段顺序修复的验证单独列于下一节：

| 层级 | 结果 |
| --- | --- |
| L0 | Ruff backend + probe、compileall、git diff --check 通过；import/private/coupling 见 L3 |
| L1 | 新增 test_entry_quantity **27 项**通过；33 个冻结样本逐值匹配 |
| L2 | planner / deferred cycle / reservation / shared cash / rebuy / manual / slice / OrderIntent / sizing 共 **269 项**通过 |
| L3 | architecture boundary / paper architecture / schema ownership / import laziness 共 **127 项**通过 |
| L4 | 完整后端 **7,591 项**通过，8 分片墙钟 **258.3 s**，最慢分片 243.3 s；前端单元 **254 项**通过 |
| 安全 | 工作树文本扫描零命中；完整本地历史扫描命中旧分支残留 20 项，逐项核对全部位置后，当前发布分支可达对象中为 0；未放宽规则或例外 |

以下微基准也是 `bdc9530` 的历史记录，不是阶段顺序修复后的时延。
性能口径为离线纯数量计算，预热 1,000 次后 30 批 × 1,000 次，基线/当时实现同进程交替
顺序，保留每批值、P50、P95、最小和最大值；不使用单次最快耗时证明改善。

| 纯数量计算（微秒 / 次） | master | PR-8 |
| --- | ---: | ---: |
| P50 | 25.882 | 28.309 |
| P95 | 36.547 | 38.527 |
| 批次最小 / 最大 | 21.476 / 40.146 | 14.279 / 45.008 |
| 每次基础风险公式执行 | 1 | 1 |
| 函数计数 | price_aware_qty × 1 | entry_quantity × 1、内部 _risk_quantity × 1 |

桌面环境负载可变；这些数值不代表生产时延。P50 约增加 2.43 微秒，
与显式结果对象和额外转发的开销一致；不宣称耗时改善。
规则维护位置从自动入口、分片模块和手动入口收敛到现有 sizing owner；
基础公式原已共用，所以重复基础计算次数仍为零，没有伪造调用次数下降。

```bash
git worktree add --detach .codex-test-tmp/pr8/base e622955deb702ae65e101ce260a0e3bea2e0a2f0
python scripts/pr8_sizing_probe.py --output .codex-test-tmp/pr8/replay.json \
  --compare backend/fixtures/entry_quantity_frozen.json \
  --benchmark-base .codex-test-tmp/pr8/base/backend/paper_sizing.py \
  --benchmark-output .codex-test-tmp/pr8/benchmark.json
```

旧路径已有共享基础公式，因此本轮不宣称减少基础计算次数或生产提速。
每个自动/手动计划仍只做一次基础风险数量计算，无额外网络或 SQL。
阶段顺序修复后，正常自动新仓路径调用 entry_quantity 4 次，适用首仓限制时 5 次；
后续均为前结果推进，基础公式仍为 1 次。手动为 1 次。未重新测量本轮端到端耗时，
不能用上述单次纯计算的历史数字代替分阶段自动路径的成本。

## PR305 阶段顺序复核（起点 bdc9530）

对照真实 master `e622955` 与审核起点，发现确实不等价，故只修改
`paper_sizing.py` / `paper_trading.py` 两处生产文件：统一入口支持同次尝试的前结果，
主编排恢复原阶段时点，没有恢复重复数量算法，也没有修改 planner 或业务门禁。

| 维度 | bdc9530 对原 master 的结果 | 修复策略 |
| --- | --- | --- |
| 纯计算 | 普通样本最终数量相同，但提前算分片/部署；损坏 recovery 输入还会跳过原本先执行的基础 sizing | 基础、试仓、首仓、分片、部署各自在原位置推进同一 owner |
| 异常优先级 | 损坏 plan + 注入画像读取错误：原版先 profile 错误，新版先分片 ValueError | 恢复画像/价格等待/派单先于分片，保留原异常传播 |
| 调用边界 | profile / price_wait / dispatch / exceptional 抛错时，新版多调用原本不会调用的 plan_builder | 这些边界未通过前，不执行分片 builder |
| 异常现场审计 | 分片异常时 local sizing / risk.sizing 字段是否已附着、已有 reasons 等不同 | 复用同一审计字典，在原时点逐步更新 |
| 分片与交易 | 16 个场景未发现原版与 bdc9530 的最终返回/持久交易投影差异 | 修复后继续逐项比较，不把“暂未发现交易差异”说成所有行为等价 |

异常现场审计指 traceback 中的内存变量，不等于已落库审计：这些异常场景由外层
写事务回滚。本轮没有证据说明 bdc9530 在这些样本里多写了成交或持久审计。

`test_entry_sizing_stage_order.py` 在**两套完整原始 backend**上运行同一组 16 场景：
单片、新分片、续片、零目标、试仓+首仓、价格等待过期+派单拒绝、builder 故障、
损坏 plan、损坏 plan+画像故障、单独画像/price_wait/dispatch/exceptional 故障、
分配提前返回、部署前设置故障、损坏 recovery 输入。

固定 DAY、_now、fixture decision_at；
`fixtures/entry_sizing_stage_order.json` 记录 git 实际 source SHA，并拒绝用其他版本
覆盖 master oracle。测试分别比较 error、调用事件、阶段观察、异常现场审计、
最终返回和持久交易投影（数量、状态、理由、分片 payload、现金、fill、预占/审计计数）。
还单独验证分阶段始终只计算一次基础公式、共享审计对象、部署后保留分片状态。

16 场景中，原始 bdc9530 有 15 个调用时序差异，1 个主异常类型/来源差异，
8 个异常现场审计差异；最终返回和持久交易投影均一致。原先的 33 组冻结矩阵继续保留，
不能用它们替代本轮新增的异常时序覆盖。

```bash
PR8_STAGE_BACKEND="$PWD/.codex-test-tmp/pr8/base/backend" \
  python backend/test_entry_sizing_stage_order.py --record backend/fixtures/entry_sizing_stage_order.json
PYTHONPATH=backend python -m unittest test_entry_sizing_stage_order
```

本轮 L0 Ruff/compileall/diff check 通过；L1 专项 2 项（16 场景）通过，连同
原 sizing/自动买入共 41 项通过；L2 自动买入/分片/执行回归 165 项通过；
L3 架构/依赖守卫 123 项通过。因生产逻辑改变，完整 L4 已重跑：
**7,593 项全部通过**，8 分片墙钟 **341.9 s**，最慢分片 322.2 s。
本轮未修改前端/API 契约，未另跑前端；完整后端回归包含既有 API 测试。
安全扫描只新增两个精确例外：离线 fixture 生成的公开 SHA256 快照/计划 ID；
不放宽任何扫描规则，也不改变旧历史残留的审计结论。

## 已知限制与回滚

最低金额、费用经济性、容量资格和实际 buying power 仍由原门禁裁决。
本地旧分支的 20 项历史敏感信息残留不属于本次发布提交链，未在本 PR 清理；
既有 dashboard.png 仍需人工图像审核，本 PR 未改图片。不能把发布范围核验说成整个本地仓库历史已清理。
通过纯 sizing 并不意味着能够成交；重验和提交必须继续读取当场证据。
审计 `target_amount` 保持历史阶段口径（例如分片后不重写），避免借重构改变业务行为。
无数据库迁移或生产配置变更。回滚整个 PR 提交/镜像，保留合法交易账本；
不要用旧数据库备份覆盖成交。PR 保持 Draft，等待人工审核，不合并、不部署。
