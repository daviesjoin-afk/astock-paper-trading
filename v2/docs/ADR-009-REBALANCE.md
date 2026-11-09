# ADR 009：资金再平衡账本事件

状态：已实现（原提案已落地，证据见验收对照表）。本轮落地 `rebalance`：在**不改写成交历史**的前提下，把现金从一槽移动到另一槽，并把当前权重持久化。ADR 008 的提案由此可以被显式执行。

## 问题

ADR 008 只产出目标权重。账户的槽位现金是从“初始预算 + 成交”派生的：如果直接改权重，历史成交会被按新预算重新解释，资金守恒与审计都会失真。

因此需要一个**追加型事件**：只移动现金、只记录决策，不修改既有成交。

## 决定

### 1. 数据模型

| 结构 | 作用 |
| --- | --- |
| `v2_paper_accounts.weights_json` | 当前权重 `[A, B]`；创建时等于 spec，之后由再平衡更新 |
| `v2_paper_transfers` | 追加型划拨记录：`from_slot, to_slot, amount_cents, applied_at, note, digest, weights_before, weights_after`，`(account_id, digest)` 唯一 |

现金派生顺序固定为：**初始预算 → 成交（按订单 id）→ 再平衡划拨（按 id）**。划拨永远后置，历史成交不重新解释。

### 2. 语义

- 权重决定**预算展示与风控口径**（单笔/单标的/现金下限都以当前权重预算为基数）；
- 划拨只移动**现金**，不移动持仓；持仓随槽位不变；
- 准备金不参与划拨：权重和必须等于 `10000 − reserve_bps`。

### 3. 执行约束（全部硬拒绝）

| 检查 | 错误码 |
| --- | --- |
| 权重在 `[min_slot_bps, max_slot_bps]`（ADR 008 默认 1500–6000） | `rebalance_weight_out_of_bounds` |
| 单次变化 ≤ `max_step_bps`（默认 1000） | `rebalance_step_exceeded` |
| 权重和 + 准备金 = 10000 | `rebalance_weights_must_keep_reserve` |
| 转出槽现金足够 | `rebalance_insufficient_cash` |
| `proposal_digest` 为 64 位十六进制 | `invalid_proposal_digest` |
| `applied_at` 为 RFC3339 | `invalid_rebalance_time` |

### 4. 幂等

- 同一 `(account_id, proposal_digest)` 且目标相同 → 返回既有记录（不重复划拨）；
- 同一 digest 但目标不同 → `rebalance_digest_conflict`。

### 5. 审计

- `budget_split` 现在按**当前权重**校验槽位预算；
- 资金守恒仍成立：划拨在两槽之间对冲，准备金与总现金不变；
- 成交、订单、会话链接检查不变。

### 6. 接口

`POST /v2/paper-accounts/{id}/rebalances`

```json
{ "targets": [3500, 5500], "proposal_digest": "<64 hex>",
  "applied_at": "2026-11-03T09:00:00+08:00", "note": "reduce momentum slot" }
```

响应为 `RebalanceRecord`（划拨方向、金额、前/后权重、摘要）。

## 验收（本轮）

1. 划拨后两槽现金按目标预算变化，总现金与准备金不变；
2. 持仓、订单数、成交不变（历史不被改写）；
3. 权重与预算在账户视图中更新，审计仍通过；
4. 越界、步长、准备金、非法摘要、重复 digest 冲突全部拒绝；
5. 同一 digest + 同一目标幂等。

## 再平衡后的下单行为（已验证）

- 槽位现金随划拨变化，下单规模按新现金计算（测试：同一信号，A 槽由 4.5M 缩到 3.0M 后成交量更小且仍为整手）；
- 准备金保持不变；划拨本身不写成交；
- 风控上限按当前权重预算计算（与规模成比例），因此“同一按比例订单”的通过/拒绝不因划拨而翻转——这与设计一致。

## 未决问题

- 划拨与 T+1 的交互：当前划拨只影响现金，不影响可卖数量；若未来允许转移持仓，需要新的可卖性规则。
- 划拨的时间语义：当前按 id 后置，未按 `applied_at` 插入历史序列；若需要严格时序，需要事件时间轴。
- 是否允许准备金参与再投资：仍不允许。
- 自动执行：当前由 API 显式触发，尚未接入调度器。
