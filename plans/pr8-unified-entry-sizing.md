# PR-8 执行计划

## 完成标准
自动、手动 BUY 的数量规则归 `paper_sizing.entry_quantity`；冻结样本数量、理由和审计逐值一致；L0–L4 通过；创建 Draft PR 后立即停止，不合并、不部署。

## 基线与清理
- PR-7 = #304，8 项 CI 成功、无未解决 review thread，按授权 squash 合并为 `e622955deb702ae65e101ce260a0e3bea2e0a2f0`。
- checkout master、pull --ff-only 后，本地 HEAD / origin/master / 远端 HEAD 均为上述 SHA，工作树 clean。
- `.agents` 是旧 PR272 clean worktree（提交已在 origin/master）、无引用且未运行的部署辅助文件；已用 git worktree remove 和逐文件删除清理。

## 修改前源码审计
自动：信号 → OrderIntent 校验 → `_buy_order` → `_price_aware_qty` → `paper_sizing.price_aware_qty` → tranche 缩放 → 首仓金额上限 → `paper_entry_decision.prepare_entry_slice` → deployment lots 上限 → 最小金额/现金费用门禁 → order/reserve → `execution_planner.commit_fill`。

手动：API → preview/confirm → `manual_orders._manual_order_plan` → `_price_aware_qty` → 同一个 `price_aware_qty` → requested<=0 取推荐量 / 超限拒绝 / 非整手拒绝 → cash/quote gate → order/reserve → `revalidate_order_plan` 重建同一计划 → `commit_fill`。

现有纯 sizing：风险止损预算、单票权重/绝对额、行业、现金、策略/池剩余额度、在途预占、流动性，取最小后整手向下取整，返回 `(qty, audit)`。另有动态最小下单金额计算，门禁应用由业务入口持有。

差异分类：
- 输入：自动有效风险画像、滑点价、独立执行缩放、流动性、分片/部署政策；手动 requested_qty、市价/限价参考价、现有风险画像，历史上没有自动缩放/流动性输入。
- 资格：自动候选评分/OrderIntent/时效/审批；手动身份授权/账户/模型复核/确认审计。不得迁入 sizing。
- 数量：共用基础约束，但自动后处理与手动请求校验散落调用方；将其迁入现有 sizing owner，保留明确输入与原顺序。
- 提交：不同 order/audit/provenance 入口，共同使用原 reservation owner 和 planner；不修改。

`reserve_shared_capital` 只预占而不扣现金，排除自身重算，跨周期 reserved BUY 仍占现金；`revalidate_order_plan` 传 remaining_qty、旧委托身份和排除自身的 reservation key；`commit_fill` 按 lease → provenance → identity → cycle → 执行模拟重算 → 原子账本写入，最终成交权威不转移给 sizing。

## 里程碑
- [x] M1：清理、合并、基线及修改前审计。
- [x] M2：冻结对照矩阵与统一接口，保留兼容转发，删除重复数量规则。
- [x] M3：L0–L4、调用计数/多轮性能对照与文档。Draft PR 为提交推送后的最后外部操作，创建后立即停止。

## 验证记录
- L0：Ruff backend + probe、compileall、diff check 通过；import cycle/private access/coupling 由 L3 一并验证。
- L1：新增 `test_entry_quantity` 27 项通过；冻结 17 基础 + 6 真实自动 + 10 真实手动，逐项匹配。
- L2：10 模块 269 项通过（planner、周期/预占、共享现金、手动、回补、分片、OrderIntent 与 sizing）。
- L3：architecture boundary / paper architecture / schema ownership / import laziness 共 127 项通过。
- L4：完整后端 7,591 项通过，8 分片，墙钟 258.3 秒，最慢 243.3 秒；前端单元 254 项通过。
- 安全工作树扫描零命中；本地全历史命中旧分支残留 20 项，各项位置完整，全部不在当前分支可达对象中；未清理无关历史或放宽规则。既有 dashboard.png 仍需人工审核，本 PR 未改图片。
- 同进程交替 30 批 × 1000 次，基础公式每次 1→1；P50 25.882→28.309 μs，P95 36.547→38.527 μs，不宣称提速。
- 计划完成后仅创建 Draft PR；不合并、不部署。
