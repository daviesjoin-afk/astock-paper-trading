# 数据接入指南（把行情变成可用估值）

目标：把一份**有来源、可复核**的行情文件变成 `reviewed` 价格集合，让估值、绩效与资金提案从 `unavailable` 变为可计算。

## 0. 先明确边界

| 规则 | 说明 |
| --- | --- |
| `trust` 必须显式 | 只有 `reviewed` 参与用户可见数字；`synthetic` 永远仅供本地测试 |
| 缺数据即拒绝 | 覆盖不完整、文件损坏、摘要不符都会让相关接口返回 `unavailable`，系统不会估算 |
| 不可就地改历史 | 已发布集合的身份（id + 摘要）被引用后不可改写，修订请发布新 id |
| 不自动联网 | 生成与安装都在本地完成；服务只读目录 |

## 1. 准备输入 CSV

列固定为：

```
symbol,day,price,source,observed_at
600000.SH,2026-10-30,12.34,official-close,2026-10-30T15:00:00+08:00
```

- `day`：`YYYY-MM-DD`，必须是该标的真实交易日；
- `price`：元，最多两位小数（内部转整数分）；
- `source`：可追溯来源标记（供应商/公告/导出文件名），不要写“unknown”；
- `observed_at`：带时区的 RFC3339，通常是收盘时刻；
- 同一标的同一天只能出现一次；区间内不得缺日（生成器会报缺口）。

模板见 `v2/data/price-set-examples/sample-prices.csv`（标注 synthetic，仅示意列结构）。

## 2. 生成 + 安装（一步）

```bash
bash scripts/publish_price_set.sh \
  --input prices.csv \
  --set-id cn-close-2026-11 \
  --pack cn-offline-fixture-pack-1 \
  --coverage-start 2026-10-30 --coverage-end 2026-11-03 \
  --symbols 600000.SH \
  --days 2026-10-30,2026-11-02,2026-11-03 \
  --trust reviewed
```

- 生成器先校验（缺口/重复/越界/非法价格），失败即中止，不会装半成品；
- 目标已存在默认拒绝覆盖（退出码 3）；确认要替换请用**新** `--set-id`，或显式 `--force`；
- 成功输出 `published set=… trust=… entries=… source_digest=…` 与后续核对命令；
- `--dry-run` 只打印计划，先于覆盖检查，不触碰任何状态。

## 3. 核对覆盖

```bash
curl -fsS "http://127.0.0.1:8765/v2/price-sets/coverage?pack_id=cn-offline-fixture-pack-1&symbols=600000.SH&days=2026-10-30,2026-11-02"
```

- `covered_pairs == 期望值` 且 `missing` 为空才算完成；
- 控制台路径：概览警告「去核对覆盖」→ 实验信息页填写数据包/标的/交易日 → 查看缺口；
- 缺口非空时，估值、绩效、资金提案会保持 `unavailable`——这是设计，不是故障。

## 4. 验证可用性

```bash
curl -fsS "http://127.0.0.1:8765/v2/health"
# reviewed_price_sets 应 ≥ 1
curl -fsS "http://127.0.0.1:8765/v2/paper-accounts/<id>/valuation?as_of_day=2026-11-03"
# status 由 unavailable 变为 valued
```

账户页应出现：估值明细、绩效行（收益/回撤/费用/笔数）、价格覆盖“完整 n/n”。

## 5. 绩效与滚动复核

```bash
python scripts/weekly_review.py --base-cycle shadow-base-1 \
  --candidate-cycle shadow-candidate-1 \
  --days-from-pack cn-offline-fixture-pack-1 --symbol 600000.SH --window-size 2
```

覆盖日不足一窗时退出 2；结论 `consistent` / `unstable` 均带逐窗理由。

## 6. 归档与复核

```bash
python scripts/db_tool.py backup --db "$ASTOCK_V2_DB" --out /backup/ledger.sqlite3
curl -fsS "http://127.0.0.1:8765/v2/evolution/events/export?from_day=2026-11-01" -o /backup/events.json
python scripts/verify_archive.py --dir /backup --manifest-out /backup/manifest.json
```

- 备份用 SQLite 在线 API，服务运行中（WAL）也安全；
- 目录校验会逐副本核对并写出合并 manifest；失败逐条报错、退出 1。

## 7. 常见拒绝与含义

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `no_reviewed_price_set` | 没有任何 reviewed 集合覆盖该请求 | 按本指南发布；或核对 `days` 是否超出覆盖 |
| `valuation_missing_price`/缺口 | 某标的某日缺价 | 补数据后重新生成新 id 集合 |
| `price_entry_outside_coverage` | 条目超出声明区间 | 修正 `--coverage-start/--coverage-end` |
| 生成器 `missing` 输出 | CSV 缺日 | 补齐该交易日或缩小声明区间 |
| `published` 失败退出 3 | 目标已存在 | 换 `--set-id` 或确认后 `--force` |

## 8. 不要做的事

- 不要把合成/示意价格标成 `reviewed`；
- 不要修改已发布集合文件内容（审计与回放会失效）；
- 不要用未来日期或未收盘价格生成“历史”集合；
- 不要跳过覆盖核对直接宣称可用。
