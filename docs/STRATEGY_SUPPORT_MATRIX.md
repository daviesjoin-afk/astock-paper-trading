# 策略支持范围表（B3/B4 收口）

基线：`codex/modularity-first-batch`，2026-10-27。用途：回答「这个策略现在到底在哪条链路上被支持」，
把"菜单隐藏了但它还能被直接调用"这类模糊状态写成可核对的四列。

本文件的表格**不是手写声明**：`backend/test_strategy_support_matrix.py` 会解析下表并与代码里的闭集逐列比对
（`strategy_registry.BUILTIN_STRATEGIES` / `LEGACY_STRATEGIES`、`strategy_roster.ROLES` / `EXIT_ONLY`、
`strategy_plugins.plugin_ids()`、`strategies.STRATEGIES`）。任何一侧漂移都会让守卫变红，因此不要手改表格去"修绿"。

## 四列含义

- **正式新入场**：能进入正式周期的**新开仓**。判据是注册表 `supports_new_cycle` + 名册角色，不是前端菜单。
- **独立研究**：研究/选股入口可以单独跑的 selector，产出的结果不构成成交授权。
- **历史回放**：回放（replay/backtest）可以按 id 运行。回放不产生晋升资格，也不冒充实时效果。
- **旧持仓退出**：只保留**退出与解释**能力（`strategy_roster.EXIT_ONLY`），不再新开仓。

## 支持矩阵

| strategy_id | 正式新入场 | 独立研究 | 历史回放 | 旧持仓退出 |
|---|---|---|---|---|
| tq_breakout | 支持 | 支持 | 支持 | — |
| trend_pullback | 支持 | 支持 | 支持 | — |
| reported_profit_breakout | 支持 | 支持 | 支持 | — |
| sector_rotation | — | 支持 | 支持 | 支持 |
| main_force_top10 | — | 支持 | 支持 | 支持 |

研究专用 selector（**不在**模拟盘名册里，永不进入正式新入场）：

| selector_id | 正式新入场 | 独立研究 | 历史回放 | 旧持仓退出 |
|---|---|---|---|---|
| three_day | — | 支持 | 支持 | — |
| five_day | — | 支持 | 支持 | — |
| ten_day | — | 支持 | 支持 | — |

注意 `strategies.STRATEGIES` 里还有 `reported_profit_breakout` 与 `main_force_top10` 两份**研究实现**，
所以这两个 id 的"独立研究"也是"支持"。其中 `main_force_top10` 的**正式新入场仍然是关闭的** ——
研究可运行不等于交易可用，两件事必须分开看。

## 为什么不是"删掉了"

`sector_rotation` / `main_force_top10` 在菜单里早已不可选，但 `strategy_plugins.get_plugin` 与
`plugins.select_candidates` 仍能按 id 直接取到实现，`strategies.run_strategy` 也仍会分派到它们。
隐藏不等于移除：真正决定"能不能成交"的是**周期参与集合**（`paper_cycle_ownership`）与生命周期，
不是前端菜单。因此在获得使用证据之前，本表把它们登记为「研究/回放保留、新入场关闭、旧持仓可退出」。

同时明确保留（不随退役删除）：

- 旧身份的历史解释与展示名（`strategy_roster`）；
- 真实资金与持仓所有权，以及未完成订单的退出路径；
- 板块强弱 / 概念扩散 / 主力确认作为**其他策略的背景确认**（`strategy_roster.EXIT_ONLY.keeps`）。

## 删除专属实现的前置证据（未取得，故本轮不删）

要删掉 `paper_trading` 里 `style == "sector"` 分支那 297 行专属实现，至少需要下面四项**真实使用证据**，
本轮无法从本机取得（生产账本在服务器上，属用户环境）：

1. 现存账户 / 周期里是否还有 `sector` 风格或 `source_strategy` 指向这两个 id；
2. 用户策略与克隆版本里是否引用了这两个 `implementation_key`；
3. 回放/研究任务在最近窗口内是否仍按这两个 id 运行；
4. 退役账户是否还有未完成订单与未退出持仓。

在拿到这些证据之前，"删除"只能是猜测；猜测会让真实的旧持仓失去退出路径。

## 2026-10-28 退役实现删除前置证据（服务器只读查询）

在 `paper_trading.sqlite3` 上只读核对：

- `paper_accounts`：`sector_rotation` / `main_force_top10` 均为 `paused`，且没有当前持仓或订单行；
- `strategy_definitions`：只有 5 个 builtin，没有 `origin=user` 的克隆/自定义策略引用这两个 `implementation_key`；
- `paper_strategy_legacy_bindings`：只有内置绑定，没有额外的用户历史绑定；
- 当前周期 `enabled_strategies` 只含三套正式策略。

因此 `paper_trading` 中 `style=="sector"` / `main_force` 的**正式新入场**分支已无正式账户可达，
可以在独立批次中删除；`strategies.py` 的研究/回放实现、`EXIT_ONLY` 退出与历史解释仍须保留。
删除前仍需补查研究/回放任务窗口（第 3 项证据），不能把"当前没有持仓"当成"从未使用"。

## 2026-10-28 B3 删除前置证据补齐（结论：保留，不删）

MD 的删除前置证据（第 3 项）已在生产库只读核对完成：

- `paper_research.sqlite3:paper_research_runs` 仍存在 `sector_rotation` / `main_force_top10` 的运行记录 → **研究/回放链路仍在使用这两个 id**，因此 `strategies.py` 的实现必须保留；
- `paper_trading` 里 `style == "sector"` / `main_force` 的候选与入场分支**仍被用户策略使用**：`strategy_creation_preview` 的风格别名表仍接受 `板块轮动`（sector rotation）与 `超强主力股`（flow momentum），用户可创建该 archetype 的策略并进入正式周期；这些账户的 `source_strategy` 是用户自定义 id，但 `style` 命中同一分支。
- 退役的两个内置账户 `sector_rotation` / `main_force_top10` 均为 `paused`，无持仓、无订单、无用户克隆引用其 `implementation_key`。

因此删除这 297 行会让**用户自建的板块轮动/主力类策略静默失去候选与入场路径**，
属于"猜测当证据"。本轮结论：**不删**；退役内置账户的新入场已被周期参与集合
（`paper_cycle_ownership`）与生命周期关闭，无需靠删代码实现关闭。

如果将来要删，前置条件是：①用户策略的 style 语义收敛到仍在役的 archetype，
或②把 sector/main_force 候选与入场实现迁到独立 owner 模块并由用户策略显式引用。
