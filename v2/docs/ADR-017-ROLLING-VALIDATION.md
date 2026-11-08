# ADR 017：滚动窗口验证

状态：提议（Draft）。把“多段独立窗口都要成立”变成硬门槛：任何一段缺覆盖、优势不足或回撤超限，都不得晋级。

## 问题

单段汇总绩效会把一次偶然的好窗口掩盖在平均值里。自进化要求“样本外稳定”，因此需要**逐窗**检查，而不是只看总收益。

## 决定

### 1. 分段绩效（只读）

`GET /v2/experiments/{cycle_id}/performance/segments?segments=2026-10-30,2026-10-31;2026-11-02`

- 每段是一组显式交易日（`;` 分隔段，`,` 分隔日）；
- 每段独立计算绩效（ADR 016 口径），并独立声明：
  - `status:"valued"` + 指标；
  - 或 `status:"unavailable"` + `reason:"no_reviewed_price_set"`（该段缺覆盖）；
- 空段 → 422 `empty_rolling_segment`；
- 响应携带 canonical 摘要。

### 2. 一致性规则

`rolling_consistency(base, candidate, margin_bps=50, max_drawdown_bps=1500)`：

1. 两侧窗口数量必须相同且非空；
2. 每一段两侧都必须 `valued`；
3. 每段 `candidate.return_bps ≥ base.return_bps + margin_bps`；
4. 每段 `candidate.drawdown_bps ≤ max_drawdown_bps`。

任一不满足 → `status:"unstable"`，附逐段理由。

### 3. 与晋级的关系

- 影子脚本 `--rolling-windows` 会取两段绩效并做一致性检查；
- `--decide` 时若滚动不一致 → 返回 `decision.status = "unstable"`，**不产生决策、不写事件**；
- 只有滚动一致时才继续走 ADR 012 的聚合指标决策与事件记录。

## 边界

- 分段仍是显式日期列表，不自动展开日历；
- 不做多重比较校正与显著性检验；
- 指标口径与 ADR 016 完全一致，没有第二套公式；
- 只读，不写账本。

## 验收（本轮）

1. 两段均覆盖 → 两段指标独立、摘要稳定；
2. 任一段缺覆盖 → 该段 `unavailable`，一致性 `unstable`；
3. 某段优势不足 → `unstable` 且理由含该窗口；
4. 某段回撤超限 → `unstable`；
5. 一致时 `--decide` 正常进入 ADR 012 流程；
6. 冒烟覆盖“合成数据 → 分段 unavailable → 一致性 unstable”。

## 未决问题

- 窗口长度/数量的自动选择（按日历与样本量）；
- 滚动训练/验证切分与再拟合（当前只做评估）；
- 结果持久化与展示（当前按需计算，不落库）。
