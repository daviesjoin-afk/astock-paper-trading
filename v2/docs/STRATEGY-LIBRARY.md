# 策略评分库

## 已实现的边界

库负责从明确的历史收盘价计算分数和原因。下单、市场状态过滤、止损、仓位大小、整手、T+1、费用及资金分配仍由原有策略政策、执行与 store 负责。

固定交易槽位与算法身份分开：

| 账本槽位（不变） | 当前绑定算法 | 实现 |
| --- | --- | --- |
| `steady` / 稳健 | `steady-v1` | `src/domain/strategy/steady.rs` |
| `aggressive` / 激进 | `aggressive-v1` | `src/domain/strategy/aggressive.rs` |

`Strategy` 是算法的统一接口；`STRATEGY_LIBRARY` 是显式注册表。现有 `score_signal(slot, bars)` 通过槽位绑定的算法 ID 调用 `score_registered(id, bars)`，真实结算已经消费这个入口。

`score_registered` 是历史长度检查的唯一入口：少于 61 条时保留原来的 `no_edge`；更长的历史完整传入，不擅自截断。store 的结算扫描原本只加载最近 61 条，这一规则仍保留。未知算法 ID 返回 `unknown_strategy:<id>`，不会回退到其他算法。

低层 `Strategy::score_closes` 接收已经通过上述检查、按时间升序排列的收盘价。新增实现应通过中央入口调用，不另建行情/时钟/网络/数据库依赖。

## 新增算法的最小改法

1. 在 `src/domain/strategy/` 新增一个实现文件，实现 `Strategy` 的 `id` 与 `score_closes`。
2. 在 `strategy.rs` 的集中注册区声明模块，并将实例加入 `STRATEGY_LIBRARY`。
3. 为算法的真实规则补行为测试；调用 `score_registered` 检查分数和原因。

这只把算法加入库。它不会新增账户或交易槽位，也不会自动使用资金。替换现有槽位算法、参数优化、候选策略影子记账、晋升和回滚属于后续独立工作；当前没有动态配置、插件加载器或运行时注册机制。

## 本次等价性证据

重构前已从真实行情库冻结 120 组输入，覆盖沪深主板、创业板、科创板，跨 2023–2026 年、61/80 条历史。`fixtures/strategy-library-golden.json` 保存实际收盘序列与旧实现的两算法分数/原因；新实现逐项匹配。另有短历史、未知 ID、注册唯一性、固定账本身份、长历史不裁剪的测试。

```bash
cd v2
cargo +1.99.1-x86_64-pc-windows-gnu test --locked --all-targets
```

普通 Linux 环境可省略 GNU 工具链选择参数。真实成交/调仓/现金和日权益的重构对照见本轮真实历史核验记录；全历史能否完成由原模型的数据与估值约束决定，评分入库不绕过这些约束。
