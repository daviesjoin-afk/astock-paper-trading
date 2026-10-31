# PR-4 风控 K 线轮内复用

基线 commit：`4ee5bf036190777c63f4b66f1d66513bbc6be14d`（PR300 squash 合并提交）。
本批只接入现有 `paper_kline_snapshot.RoundKlineCache`，治理风险证据与审计的重复日线读取。
不混入 PR-5 替补候选 SQL 批量化或 execution marker 合并。

## 事实归属与截止语义

- 每次 `paper_risk_service.run` 创建一个现有缓存实例，结束后丢弃；不缓存交易结论，不跨轮次或进程保留。
  同轮不同账户对同一证券可共用原始日线，但分别做风险判断。
- 服务将证券转为字符串、截止日规范为日期；key 仍为 `(证券, 截止日, inclusive)`。
  日期对象与同日 ISO 文本共用 key；不同日期、证券或包含边界互不代替。
- 风险模型、持仓天数和波动观察用 `inclusive=False`，审计缺省补读用 `inclusive=True`。
  在 writer 前分别预取持仓的两种边界，绝不从排除当日的结果冒充包含当日的结果。
- 截断仍由原 `data_fetcher.completed_kline` 完成，PIT（只使用截止当时已知的数据）语义保持；
  未来行、不可解析的日期仍按原逻辑排除。源属性不重写，每个消费者取得独立 DataFrame 副本。
- 仅用 `dataclasses.replace` 组装本轮 ports 副本，不改调用者的端口，不注入全局缓存。

## 预取的空值与异常交接

原 owner 只复用成功且非空的结果，空值与失败允许后续重试。本批保留此规则。
新增的可选 `prefetch` 提前执行第一次读取；若为空或异常，保存的是**尚未消费的一次读取结果**，
首个真实消费者原样取得空值或抛出同一异常，随后移除该交接状态，后续消费者再按原路径重试。
这样不会把“第一次读失败”吞掉后悄悄变成成功，也不会把失败固定为整轮结果。
没有调用 `prefetch` 的既有手动订单缓存路径保持原行为。

例如 loader 顺序是 `None → 有效日线`：预取取得 None，首个趋势消费者仍看到 None；
后续持仓天数消费者才重试取得日线。若第一次抛出异常，风险消费者仍使该轮失败。
未被任何消费者使用的审计边界异常不提前使风险失败，审计真实补读时仍由其原异常处理决定结果。

## 写事务边界

预取在市场证据采集完成后、SQLite immediate writer（立即取得写入资格的事务）之前完成。
成功命中的风险证据和审计读取不再进入 CSV/Pandas 文件 I/O。
writer 内仍重新核对 cycle、positions、lease/fencing，预取期间失去运行资格的旧进程不得成交。

不宣称所有路径零文件 I/O：空/失败后的重试、writer 重验新增的持仓仍沿原路径惰性读取；
换仓买入和后续手动订单是独立应用流程，未改成共享这个风险缓存。
不通过返回假空值、跳过新持仓或弱化执行复核来减少读取。

## 定向与真实 CSV 对照

新增 `backend/test_risk_round_kline.py` 覆盖日期与包含边界、PIT、消费者改写副本、跨账户及跨轮、
空值/空表/异常的首次消费与恢复、日期文本审计复用、writer 新持仓、周期切换、租约移交与过期。
真实 SQLite 第二连接的 `BEGIN IMMEDIATE` 用于验证每次源读取时是否持有 writer。
相关六模块 87 项通过；架构与交易边界 106 项通过。Ruff、compileall、diff check 通过，未放宽架构预算。

`scripts/pr4_risk_kline_probe.py` 在基线和修改版 checkout 根目录运行同一脚本，固定时钟、固定行情响应、
独立临时账本和 CSV，真实执行 `monitor_risk` 与 `pandas.read_csv`。
13 个场景的风险返回、异常类型/内容和 14 表全部内容一致，共 355 行、5,275 字段。
包含持有、全卖、部分卖出、旧报价、T+1、跌停、同证券双账户、缺失、空表、损坏、
暂时空值、暂时异常及未消费的审计异常。规范化结果 JSON SHA256：
`c19321b97ec2ae2f926ff8cc1e7fcf081b594d2afa29745daf29187a8f26e422`。

| 固定场景 | 目标证券 CSV 读取 前→后 | writer 内目标 CSV 读取 前→后 |
| --- | ---: | ---: |
| 持有 / 旧报价 / T+1 | 4→2 | 4→0 |
| 全卖 / 部分卖出 / 跌停 | 5→2 | 5→0 |
| 同证券双账户 | 10→2 | 10→0 |
| 文件缺失 | 0→0 | 0→0 |
| 空表 | 5→6 | 5→4 |
| 损坏 CSV | 1→2 | 1→0 |
| 第一次空值后恢复 | 4→2 | 4→1 |
| 第一次读取异常 | 0→1 | 0→0 |

只计目标证券的实际 CSV 解析，不将指数等其他源入口算成重复目标文件。
预取包含截止日边界有时增加一次未消费尝试；优先保持异常和空数据语义，不声称每个场景都减少读取。
这些是离线读取与事务归属计数，不是生产 HTTP 次数或整轮提速比例；未测线上 P50/P95、锁等待与风险间隔。

复跑时 freezegun 仅作验收工具依赖，不加入应用依赖：

```bash
PYTHONPATH=backend python -m unittest test_risk_round_kline test_risk_application_service \
  test_manual_order_kline_replay test_risk_alt_evidence_prefetch \
  test_risk_input_demand test_paper_risk_exit_production_path
python /absolute/path/to/scripts/pr4_risk_kline_probe.py /temporary/path/result.json
python scripts/run_tests_parallel.py --shards 8 --timeout 900
```

另以原 `scripts/pr298_ledger_probe.py` 对照已批准委托恢复、重复成交、周期与运行资格等黄金场景，
10 个场景、17 个检查点的返回及 12 表全字段一致。
最终本地完整后端 7,491 项全部通过，Python 3.14.5，8 分片 326.5 秒，无失败、错误或模块加载错误。
远端最终 HEAD 的 CI 实际日志同步记录在本 PR 描述。

## 发布与回滚

无 schema 或业务数据迁移；不改阈值、行情政策、资金、T+1、唯一成交权威或调度频率。
回滚恢复原代码/镜像，保留合法交易账本，不用旧备份覆盖成交。
PR300 被 squash 的公开修复提交编号仅作精确安全例外登记，扫描规则不变。

MERGE: NOT MERGED（本 PR）

DEPLOY: NOT DEPLOYED
