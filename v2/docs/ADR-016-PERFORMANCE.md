# ADR 016：账户绩效摘要

状态：提议（Draft）。基于**完整审核净值序列**派生收益、回撤、费用与成交笔数；缺覆盖则整份不可用。

## 决定

`GET /v2/paper-accounts/{id}/performance?days=a,b`

先取 ADR 007 的 `EquitySeries`（要求 `reviewed` 集合覆盖所有请求日与持仓标的），再计算：

| 字段 | 定义 |
| --- | --- |
| `start_equity_cents` / `end_equity_cents` | 首日 / 末日权益 |
| `profit_cents` | 末日权益 − 初始本金 |
| `return_bps` | `profit × 10000 / 初始本金`（整数 bps） |
| `max_drawdown_cents` / `max_drawdown_day` | 序列峰值到谷底的最大回撤 |
| `drawdown_bps` | `max_drawdown × 10000 / 峰值` |
| `fees_cents` / `trades` | 全部成交的费用与笔数（来自成交事实） |
| `price_set_id` / `digest` | 使用的价格集合与报告摘要 |

无覆盖 → `{status:"unavailable", reason:"no_reviewed_price_set"}`；空日期列表 → 422。

## 边界

- 不读市价以外的外部信息；不插值、不裁剪；
- 不输出年化、夏普、胜率（需要年化约定与样本量声明）；
- 只读，不写账本；
- 合成集合永不产生报告（选择器只接受 `reviewed`）。

## 验收（本轮）

1. 两日序列下收益、回撤、费用、笔数与序列/成交一致；
2. 同一输入重复请求与摘要一致；
3. 合成集合与缺覆盖 → `unavailable`；
4. 空日期 → 422；
5. 账户页在净值可用时展示 `收益 / 回撤 / 费用 / 笔数`。

## 实验绩效（已实现）

`GET /v2/experiments/{cycle_id}/performance?days=`：

- 与账户口径一致，但使用**实验成交**（`v2_fills` 经 `v2_simulation_runs` 关联）与实验初始本金；
- 价格集合必须覆盖全部请求日与持仓标的；
- 返回 `cycle_id` 与同一组指标（收益/回撤/费用/笔数/digest）；
- 影子候选因此可以在同一口径下与基准比较，作为 ADR-012 决策输入（当审核数据覆盖其区间时）。

## 未决问题

- 时间加权/资金加权收益：当前是简单期末差额；
- 费用是否单列手续费与税费；
- 净值序列持久化与日更（当前按需重算）；
- 年化与风险调整指标。
