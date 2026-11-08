# 审核价格集合：工件格式与审核流程

本目录存放**已审核**的 A 股收盘价集合（`*.json`）。服务只读加载，不写入、不修改、不补全。目录为空时，账户估值与净值序列一律返回 `unavailable`。

## 文件格式

一个文件 = 一个 `PriceSet`：

```json
{
  "schema_version": 1,
  "price_set_id": "cn-close-2026-11",
  "market_data_pack_id": "cn-offline-fixture-pack-1",
  "trust": "reviewed",
  "coverage_start": "2026-10-01",
  "coverage_end": "2026-10-31",
  "entries": [
    {
      "symbol": "600000.SH",
      "day": "2026-10-30",
      "price_cents": 130000,
      "source": "vendor-close",
      "observed_at": "2026-10-30T15:00:00+08:00"
    }
  ],
  "source_digest": "<64 hex>"
}
```

### 字段规则

| 字段 | 规则 |
| --- | --- |
| `schema_version` | 必须为 `1` |
| `price_set_id` | 全局唯一（跨文件重复会被拒绝） |
| `market_data_pack_id` | 必须与账户槽位绑定的数据包一致 |
| `trust` | 仅 `reviewed` 可用于用户估值；`synthetic` 只允许出现在测试/示例 |
| `coverage_start/end` | 合法的 `YYYY-MM-DD`，且 `start <= end` |
| `entries[]` | 非空；`(symbol, day)` 不得重复；价格为正整数分；`day` 必须落在覆盖区间内 |
| `source_digest` | 生成该集合的原始数据摘要（审核留痕） |

价格单位为**分**（整数）。不接受浮点、不接受缺失值、不做插值。

## 审核清单（发布前必须逐条确认）

1. **来源**：数据供应商、接口、抓取时间、审核人；
2. **覆盖**：起止交易日与证券清单，是否包含停牌日；
3. **复权口径**：不复权 / 前复权 / 后复权，跨除权日是否可比；
4. **停牌处理**：停牌日是否给出最后成交价，还是留空（留空则该日不可估值）；
5. **字段一致性**：`source_digest` 可由原始数据重算；
6. **命名与版本**：`price_set_id` 唯一且可追溯到审核记录。

## 加载与选择

- 服务启动后按请求读取 `ASTOCK_PRICE_SETS_DIR`（默认 `data/price-sets`）下的所有 `*.json`；
- 全部文件解析成功才会形成目录；任一文件损坏或 id 重复 → 整体报错，不静默跳过；
- 选择规则：`trust = reviewed` + 数据包匹配 + 覆盖请求日期（序列要求覆盖整段区间）；
- 每次响应携带 `price_set_id` 与内容摘要，便于复核。

## 合成数据

`trust: "synthetic"` 的集合**永远不会**被用户估值选中，也不会产生盈亏字段；它只用于离线回归与文档示例。禁止把合成价格标成 `reviewed`。
