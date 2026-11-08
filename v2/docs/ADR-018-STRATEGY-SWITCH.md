# ADR-018: 策略版本切换（设计）

状态：已实现（schema v15：写端点、订单归因与时序、实验派生会话版本校验、视图与审计）｜日期：2026-11-08｜依赖：ADR-005、006、012

## 背景

账户槽位在创建时固定 `strategy_version_id`，会话必须与该版本一致；进化事件只做记录，不会改变账户。于是“晋级后让账户真的用上新版本、必要时回退”目前没有接线。

两种错误做法：

1. 直接改写槽位版本并重算历史——等于篡改账本，审计与回放都会失效；
2. 让晋级事件自动改账户——把研究判定与资金执行耦合成隐式副作用，无法解释也无法回退。

## 决定

引入**显式、追加式**的策略切换记录，槽位当前版本由记录链推导。

### 数据

新增只追加表 `v2_paper_slot_switches`（schema v15，加入严格白名单）：

| 列 | 含义 |
| --- | --- |
| `switch_id` | 客户端提供、幂等键 |
| `account_id` / `slot` | 目标槽位 |
| `from_version` / `to_version` | 版本迁移；`from` 必须等于链上当前版本 |
| `effective_from` | 显式生效时刻（调用方给出，核心不读机器时钟） |
| `reason` | 必填的人类可读理由 |
| `digest` | canonical 摘要，覆盖除自身外全部字段 |

写入方法 `switch_slot_strategy(...)` 在 `Store`（唯一写者）：

- `to_version` 必须是同 `market_data_pack_id` 下已审核的策略工件；
- `effective_from` 必须晚于该槽位最后一次会话/成交时刻；
- 同一 `switch_id` 重放必须幂等，内容不同则冲突；
- 不移动资金、不改权重、不生成订单。

### 读取

- 账户视图与总览新增 `slots[].strategy_version_id`（当前）与 `slots[].switch_count`；
- 新增 `GET /v2/paper-accounts/{id}/strategy-switches`（只读、按时间倒序、可分页）；
- 审计新增第 9 项 `switch_chain`：`from` 连续、`to` 为已审核版本、`effective_from` 单调。

### 与会话/订单的关系

- 会话创建时固定 `strategy_version_id`，必须等于**创建时刻**的当前版本；
- 既有成交与拒绝保留原版本，不回溯改写；
- 切换后新会话自然使用新版本；旧持仓继续按原成本与规则管理（估值不受版本影响）。

### 回退

回退 = 一次反向切换记录（`from` = 当前，`to` = 旧版本），理由必填；不删除任何历史。

## 后果

- 账户具备“版本演进史”，但历史不可被重写；
- 晋级事件与切换动作解耦：事件提供证据，切换是显式操作；
- 审计能证明“账户在什么时间用什么版本交易”。

## 非目标

- 晋级后自动切换（仍由人或外部调度显式调用）；
- 同一槽位并行多版本（影子实验继续在实验域运行）；
- 按版本拆分绩效归因（后续 ADR）。

## 实现进度（schema v15，已完成）

- 表 `v2_paper_slot_switches` 与 `Store::record_strategy_switch`：版本连续、幂等、时间单调、理由必填、canonical 摘要，且 `effective_from` 必须晚于该账户最后一次会话 `clock_at`；
- `GET|POST /v2/paper-accounts/{id}/strategy-switches`（`slot=A|B` 过滤；POST 体为 `slot/to_version/effective_from/reason/switch_id`）；
- 订单：归因、幂等键与请求内 `strategy_version_id` 全部使用“当前版本”；`decision_at` 早于最后一次切换 → 409 `paper_order_before_strategy_switch`；
- 实验派生会话（`from-experiment`）校验实验版本等于“当前版本”；
- 会话起点不得早于该槽最后一次切换（`session_starts_before_strategy_switch`），否则其信号会逐条被订单时序校验拒绝，形成“卡住的会话”；
- 视图 `current_strategy_version_id` / `switch_count`；审计 `switch_chain`（末项）；
- 普通会话规格不带版本（版本在成交时按当时链确定），这是既有设计，不在本 ADR 改动。

## 待解问题

1. 切换是否要求槽位无持仓？本 ADR 选择“允许有持仓，仅限制新会话起点”，需在实现时确认 T+1 与估值一致性；
2. ~~版本切换是否应触发一次资金再平衡评估~~ → 已定：不自动触发；但资金提案会检测“窗口内发生切换”并在对应槽位理由后追加“历史绩效来自旧版本”，同时重算提案摘要，避免把旧版本业绩记到新版本头上。
