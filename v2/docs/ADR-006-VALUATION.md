# ADR 006：账户估值与审核价格集合

状态：提议（Draft）。本轮只落地**估值契约与拒绝语义**：没有审核价格集合时，账户只报告“无法估值”及其所需数据；不计算市值、净值、收益或回撤。

## 问题

ADR 004 已禁止在缺少审核估值价格时输出收益类指标。账户与槽位现在只有现金、成交与持仓数量，没有任何市值概念。要继续 MVP 2（按绩效调整资金）与 MVP 3（自进化），必须先定义：

- 什么样的价格集合可以被信任；
- 估值在什么时点、覆盖哪些标的是合法的；
- 数据缺失时如何拒绝，而不是用近似值填补。

## 决定

### 1. 审核价格集合（Reviewed Price Set）

一个价格集合必须显式声明：

| 字段 | 含义 |
| --- | --- |
| `price_set_id` + `schema_version` | 内容身份与契约版本 |
| `market_data_pack_id` | 绑定的行情包（ADR 002） |
| `coverage` | 起止中国交易日（含首尾） |
| `symbols[]` | 覆盖的证券清单；不在清单内的持仓不可估值 |
| `entries[]` | 每个 (symbol, day) 一条收盘价：整数分、来源、观察时刻、源证据摘要 |
| `source_digest` | 生成该集合的原始数据摘要 |
| `trust` | `reviewed` 或 `synthetic`；`synthetic` 永远不能产生收益类指标 |

价格集合是**数据工件**，不是运行时写接口的产物：由离线流程生成、审核并随版本发布；服务只读加载，不提供在线写入。

### 2. 估值规则（fail-closed）

给定账户与显式 `as_of_day`：

1. 账户必须存在；日期必须是合法的中国交易日（格式与日历规则同其它 as-of 接口）。
2. 必须存在与账户槽位数据包一致的 `reviewed` 价格集合，且覆盖 `as_of_day`。
3. 集合必须覆盖**所有持仓标的**；任一缺失 → 整体 `valuation_unavailable`，不输出部分市值。
4. 全部满足才输出：
   - 每个槽位的持仓数量、收盘价、市值（整数分）；
   - 账户权益 = 准备金 + 两槽现金 + 两槽市值；
   - 费用合计与成交笔数（来自成交事实）；
   - 盈亏 = 权益 − 初始本金（仅在价格集合为 `reviewed` 时提供）。
5. 回撤、夏普等需要**日序列**的指标不在本 ADR 范围；它们必须由后续“净值序列”ADR 定义，且只能基于同一价格集合。

### 3. 合成价格

- `trust = synthetic` 的集合仅用于离线回归与 UI 测试。
- 使用合成集合时，报告必须带 `trust: "synthetic"`，并且**禁止**输出盈亏、收益率、回撤等任何绩效字段；只允许出现在测试与文档示例中。
- 合成集合不得冒充真实行情，也不得进入进化晋级依据。

### 4. 拒绝语义（本轮已实现）

`GET /v2/paper-accounts/{id}/valuation?as_of_day=YYYY-MM-DD`

当前没有价格集合时返回：

```json
{
  "status": "unavailable",
  "reason": "no_reviewed_price_set",
  "requirements": [{"slot": "A", "market_data_pack_id": "...", "symbols": ["600000.SH"]}],
  "missing": ["reviewed_price_set"],
  "priced_symbols": 0,
  "digest": "…"
}
```

- 不含市值、净值、盈亏字段；
- `requirements` 明确列出需要覆盖的 (数据包, 标的)；
- `digest` 对整份报告做 canonical 摘要，同一账户 + 同一日期在重启后完全一致；
- 非法日期 → 422 `invalid_valuation_day`；账户不存在 → 404。

### 5. 只读

估值只读取账户、成交与价格集合；不写账户、不写价格集合、不改变会话。快照可由调用方按需重算。

## 验收（本轮）

1. 没有价格集合时，端点只返回 `unavailable` + 需求清单，绝不包含市值/盈亏字段；
2. 需求清单与账户槽位、持仓标的、数据包一致；
3. 同一账户 + 同一 `as_of_day` 的报告与 `digest` 在服务重启后一致；
4. 非法日期与缺失账户有明确错误码；
5. 前端展示“估值快照不可用”及所需价格，不显示任何收益数字。

## 实现状态

- **已实现（纯域 + Store）**：`PriceSet` 校验（版本、信任级别、覆盖区间、逐条价格、重复条目）、`value_account()` 标记市值、权益、`profit_cents`（仅 `reviewed`），以及 `Store::paper_valuation_with_set()` 从槽位成交派生持仓并按数据包绑定校验。
- **已实现（HTTP/UI）**：无价格集合时的拒绝路径与需求清单（ADR 006 §4）。
- **已实现（目录与选择）**：bundled `data/price-sets-registry.json`（当前为空）、`GET /v2/price-sets` 只读目录、`PriceSetRegistry::select()` 仅选择 `reviewed` 且覆盖日期且绑定数据包的集合；HTTP 估值在选中集合时返回 `status:"valued"` 与 `valuation{市值,现金,权益,盈亏}`，否则维持 `unavailable`。
- **已实现（运行时目录）**：价格集合改为从 `data/price-sets/*.json` 只读加载（可用 `ASTOCK_PRICE_SETS_DIR` 覆盖），空目录/缺失目录 = 无数据；文件损坏或 id 重复 → 整体报错，不静默跳过。工件格式与审核清单见 `v2/data/price-sets/README.md`。
- **已实现（数据运维）**：`research/make_price_set.py` 生成（缺口/重复/越界即失败）、`scripts/publish_price_set.sh` 安装（默认拒绝覆盖、支持 dry-run）、`GET /v2/price-sets/days` 覆盖日、`GET /v2/price-sets/coverage` 缺口清单（仅统计 `reviewed`）、实验信息页目录与覆盖核对面板、`data/price-set-examples/` 输入模板（标注 synthetic）。
- **仍未闭合**：真实 `reviewed` 行情数据本身（目录当前为空，刻意不伪造）——这是外部依赖，不是代码缺口。
- **约束**：无 `reviewed` 覆盖时 HTTP 端点一律 `unavailable`，不返回任何估值数字；`synthetic` 集合永远不参与用户可见数字。

## 未决问题

- ~~价格集合的存放位置~~ → 已定：`data/price-sets/*.json` 由 Store 只读加载（`ASTOCK_PRICE_SETS_DIR` 可覆盖），摘要随工件发布；见实现状态。
- 停牌、除权除息、涨跌停时如何选取收盘价（需要明确“最后成交价”规则）。
- 净值日序列的生成与持久化：目前倾向按需重算，不新增写入者。
- 真实数据源接入与审核流程：需要单独的数据契约 ADR。
