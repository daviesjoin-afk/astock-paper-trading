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

查看槽位策略版本切换链（只读）：

```bash
curl -fsS "http://127.0.0.1:8765/v2/paper-accounts/paper-001/strategy-switches?slot=A"
```

返回按生效时间升序的追加记录；空数组表示该槽仍使用创建时的版本。

执行一次切换（追加式，不移动资金）：

```bash
curl -fsS -X POST "http://127.0.0.1:8765/v2/paper-accounts/paper-001/strategy-switches"   -H 'content-type: application/json'   -d '{"slot":"A","to_version":"research-momentum-v2","effective_from":"2026-11-10T09:00:00+08:00","reason":"AD-012 promoted candidate","switch_id":"switch-2026-11-10-a"}'
```

- `effective_from` 必须晚于该账户最后一次会话 `clock_at`；
- 同一 `switch_id` 重放返回原记录，内容不同则 409；
- 切换后提交 `decision_at` 早于该时刻的订单会被拒绝（`paper_order_before_strategy_switch`）；
- 会话 `start_at` 早于该槽最后切换时刻会被拒绝（`session_starts_before_strategy_switch`），避免创建“永远走不动”的会话。

归档进化事件（只读导出，含 canonical 摘要）：

```bash
curl -fsS "http://127.0.0.1:8765/v2/evolution/events/export?from_day=2026-11-01&to_day=2026-11-30"   -o evolution-2026-11.json
```

响应含 `count`、`events[]` 与 `digest`（对除 digest 外的内容做 canonical 摘要）；同一过滤条件重放应得到同一 `digest`。响应含 `count`、`events[]` 与 `digest`（对除 digest 外的内容做 canonical 摘要）；同一过滤条件重放应得到同一 `digest`。

离线复核（不依赖服务）：

```bash
python scripts/verify_export.py evolution-2026-11.json
# verified count=42 digest=…
```

退出码：0 通过、1 摘要/计数不一致、2 文件不可读或非法 JSON。退出码：0 通过、1 摘要/计数不一致、2 文件不可读或非法 JSON。

账本与导出一起归档时，用联合校验（输出合并 manifest 摘要）：

```bash
python scripts/verify_archive.py --db /backup/astock.sqlite3 --export evolution-2026-11.json
# archive ok schema=15 accounts=2 events=4 export_count=4 manifest=…
```

退出码：0 全部通过、1 任一产物无效、2 文件缺失或不可读。退出码：0 全部通过、1 任一产物无效、2 文件缺失或不可读。



整目录校验（`<name>.sqlite3` 配 `<name>.export.json` 或 `<name>.json`，可选 `<name>.manifest.json` 侧车会一并核对）：

```bash
python scripts/verify_archive.py --dir /backup/astock
# pair.sqlite3 ok manifest=…
# all 1 archives verified
```

任一副本缺配对或摘要不符 → 退出码 1，并逐条列出原因。加 `--manifest-out F` 会在校验后写出 `{archives[], combined, failed}`；`verify_all --archive D --report R` 会自动把它写到 `R.archive.json`。加 `--manifest-out manifest.json` 会在校验通过后写出侧车清单（两文件 SHA-256、schema、事件数与合并 manifest），失败时**不写**，便于归档目录自证。

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

0. 先用样例模板确认列结构（`v2/data/price-set-examples/sample-prices.csv`，注明 synthetic、非行情），命令见该目录 README。
1. 供应商 CSV → 生成 + 安装一步完成（缺口默认失败，已存在的工件需 `--force` 才覆盖）：

```bash
bash scripts/publish_price_set.sh --input prices.csv --set-id cn-close-2026-11   --pack cn-offline-fixture-pack-1 --coverage-start 2026-10-01 --coverage-end 2026-10-31   --symbols 600000.SH --days 2026-10-30,2026-11-02 --trust reviewed
```

`--dry-run` 只打印计划；目标已存在时拒绝覆盖（退出码 3）；成功后打印 `published set=… source_digest=…` 与后续核对命令。

2. 按 `data/price-sets/README.md` 审核来源、覆盖、复权口径与停牌处理；
3. 将 JSON 放入 `ASTOCK_PRICE_SETS_DIR`（默认 `data/price-sets`）；
4. `GET /v2/price-sets` 核对 `price_set_id`、信任级别与摘要；
5. 回到账户页确认估值由 `unavailable` 变为 `valued`；失败时按 `missing` 清单补数据。

发布前可用覆盖报告核对缺口：

```bash
curl -fsS "http://127.0.0.1:8765/v2/price-sets/coverage?pack_id=cn-offline-fixture-pack-1&symbols=600000.SH,000001.SZ&days=2026-10-30,2026-11-02"
```

返回 `covered_pairs` 与 `missing[]`（只统计 `reviewed` 集合）；缺口不为空时不要指望估值/绩效可用。

## 健康检查

```bash
curl -fsS http://127.0.0.1:8765/v2/health
```

返回 `schema_version`、`accounts`、`sessions_active`、`price_sets`（总数）、`reviewed_price_sets`、`synthetic_price_sets`、`paper_only`、`writer`。`reviewed_price_sets == 0` 时估值/绩效/资金提案必然不可用。

## 一键验证

```bash
bash scripts/verify_all.sh                 # 全量：Rust / 研究 / 冒烟 / 浏览器 / 泄露扫描
bash scripts/verify_all.sh --fast          # 跳过浏览器
bash scripts/verify_all.sh --dry-run       # 只打印将执行的命令
bash scripts/verify_all.sh --archive /backup/astock   # 附带整目录归档校验
bash scripts/verify_all.sh --report /tmp/verify.json  # 输出 JSON 汇总（含每套件耗时）
bash scripts/verify_all.sh --only research,smoke     # 只跑指定套件
```

输出按套件打印 `result` 与耗时；任一失败退出码 1。Windows（MSYS）默认使用 `cargo +1.99.1-x86_64-pc-windows-gnu`，其他平台用 `cargo`；可用环境变量 `CARGO`/`PYTHON` 覆盖。浏览器套件需要先有 release 二进制（脚本内会 `npm run build`）。

## 备份与恢复

```bash
python scripts/db_tool.py backup --db "$ASTOCK_V2_DB" --out /backup/astock.sqlite3
python scripts/db_tool.py verify --db /backup/astock.sqlite3
python scripts/db_tool.py restore --backup /backup/astock.sqlite3 --db /srv/astock/restored.sqlite3
```

- 使用 SQLite 在线备份 API，服务运行中（含 WAL）也可安全备份；不依赖 `sqlite3` CLI。
- `backup` 拒绝覆盖已有文件（退出码 3），成功后打印 `schema/accounts/events/sha256`。
- `restore` 先校验备份（完整性 + schema），目标已存在同样需要 `--force`；恢复前清理陈旧 `-wal/-shm`。
- `verify` 校验完整性、`user_version` 与关键表计数：0 通过 / 1 校验失败 / 2 使用或 IO 错误。
- 账本、会话游标、划拨与进化事件全部来自数据库；会话推进幂等，重启后从 `cursor` 继续。

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
