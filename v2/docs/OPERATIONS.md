# V2 运行手册（本地/单机）

## 写入模型

- 只有一个进程、一个 `Store`，所有写入经同一个 SQLite 连接串行化（WAL + busy_timeout）。
- 结论：并发提交同一信号不会产生重复成交。
  - 冒烟测试：8 个线程并发 POST 同一订单 → 恰好 1 笔成交、7 个 `duplicate=true`、`orders_total=1`、审计通过。
- 不要横向启动多个写进程指向同一个数据库文件；只读副本请用 SQLite 备份文件。

## 健康检查

`GET /v2/health`

```json
{"status":"ok","system":"astock-v2","paper_only":true,
 "schema_version":13,"price_sets":0,"writer":"store-single-writer"}
```

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 数据库实际 schema 版本；与代码不一致时服务启动会直接拒绝 |
| `price_sets` | 已加载的审核价格集合数量；0 表示估值/净值/资金提案都会返回 `unavailable` |
| `writer` | 固定为单写者模型 |

## 定时编排（示例）

历史模拟不依赖内部时钟；由外部 cron/任务系统按交易日调用：

```bash
# 每交易日 15:30（中国时区）推进账户到当天
30 15 * * 1-5 /opt/astock/v2/scripts/daily_advance.sh   --account paper-001 --day "$(TZ=Asia/Shanghai date +%F)"
```

脚本行为：

- `--day` 必填（`YYYY-MM-DD`），非法格式退出码 2；
- `--dry-run` 只打印请求，不写账本；
- 请求失败（HTTP 非 2xx）退出码 3；
- 成功输出 `advanced account=… day=… sessions=… steps=… filled=… rejected=…`，可直接作为调度日志。

每周复核（只读，不写账本）：

```bash
0 16 * * 5 /opt/astock/v2/scripts/weekly_review.py   --base-cycle shadow-base-1 --candidate-cycle shadow-candidate-1   --windows "2026-10-30,2026-10-31;2026-11-02"
```

- 退出码 0 = 一致（可继续走治理决策）；4 = 不稳定（缺覆盖/优势不足/回撤超限）；2 = 参数或请求错误；
- 输出逐窗基准/候选收益与回撤，以及最终结论与理由。

也可以只给出交易日清单，由脚本按固定窗口切分（只保留完整窗口）：

```bash
python scripts/weekly_review.py --base-cycle base --candidate-cycle cand   --days 2026-10-30,2026-10-31,2026-11-02,2026-11-03 --window-size 2
```

不足一个完整窗口、或 `--window-size` 非正数时退出码 2。

第三种方式：直接用**已审核价格集合覆盖的交易日**生成窗口（无审核数据时无法建窗，退出码 2）：

```bash
python scripts/weekly_review.py --base-cycle base --candidate-cycle cand   --days-from-pack cn-offline-fixture-pack-1 --symbol 600000.SH --window-size 2
```

覆盖日由 `GET /v2/price-sets/days?pack_id=&symbol=` 提供，只统计 `reviewed` 集合。


- 只推进 `running` 会话；暂停的会话保持暂停。
- 返回 `{sessions, steps, filled, rejected}`，可直接作为调度日志。
- 资金再平衡不在推进中自动执行：先取提案（`/allocation-proposal`），人工/策略确认后再 `POST /rebalances`。

## 价格集合发布

1. 供应商 CSV → `python research/make_price_set.py ...`（缺口默认失败）；
2. 按 `data/price-sets/README.md` 审核来源、覆盖、复权口径与停牌处理；
3. 将 JSON 放入 `ASTOCK_PRICE_SETS_DIR`（默认 `data/price-sets`）；
4. `GET /v2/price-sets` 核对 `price_set_id`、信任级别与摘要；
5. 回到账户页确认估值由 `unavailable` 变为 `valued`；失败时按 `missing` 清单补数据。

发布前可用覆盖报告核对缺口：

```bash
curl -fsS "http://127.0.0.1:8765/v2/price-sets/coverage?pack_id=cn-offline-fixture-pack-1&symbols=600000.SH,000001.SZ&days=2026-10-30,2026-11-02"
```

返回 `covered_pairs` 与 `missing[]`（只统计 `reviewed` 集合）；缺口不为空时不要指望估值/绩效可用。

## 备份与恢复

```bash
sqlite3 "$ASTOCK_V2_DB" ".backup '/backup/astock-$(date +%F).sqlite3'"
```

- 不要在 WAL 模式下直接复制 `.sqlite3` 文件（可能缺少 `-wal` 内容）。
- 恢复：停服务 → 替换数据库文件 → 启动；账本、会话游标、划拨与进化事件全部来自数据库。
- 会话推进是幂等的：重启后从 `cursor` 继续，不会重复成交。

## 升级与回滚

- schema 版本不匹配时服务拒绝启动（`database_identity_or_schema_version_mismatch`），没有就地迁移。
- 升级：备份 → 用新版本启动空库 → 按需重放/重建账户；旧库保持只读备查。
- 回滚：换回旧二进制 + 旧库文件；两者必须成对。

## 故障排查

| 现象 | 检查 |
| --- | --- |
| 估值/净值/提案全部 `unavailable` | `price_sets` 是否为 0；目录、文件名、`trust`、覆盖日期 |
| 订单被拒 `risk_*` | 账户页“风控”行与该槽位现金、单标的成本占比 |
| 审计失败 | 展开“审计详情”，按失败项定位（成交/订单不一致、划拨链断裂等） |
| 推进无动作 | 会话是否 `running`；`day` 是否早于下一条信号 |
